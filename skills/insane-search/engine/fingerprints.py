"""
Adversarial Fingerprint Evolution — per-domain mutation engine.

Every request gets a slightly different browser fingerprint so no single
pattern enters a detection database. Successful mutations are tracked
per domain and reused; failed ones are discarded.

Concept: Sentinel 2026 shows fixed fingerprint reuse is the single largest
detection signal. Antidetect browsers (Kameleo, Multilogin) solve this
statically with pre-built profiles. This module evolves fingerprints dynamically.

No-Site-Name Rule: domains are stored as hashes, not plaintext.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import tempfile
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit


OBS_DIR = os.path.join(tempfile.gettempdir(), ".insane_observations")


def _host_hash(url: str) -> str:
    return hashlib.sha1(
        (urlsplit(url).hostname or "unknown").encode()
    ).hexdigest()[:12]


@dataclass
class Fingerprint:
    """Mutable browser fingerprint parameters."""
    viewport_width: int = 1366
    viewport_height: int = 900
    languages: list[str] = field(default_factory=lambda: ["en-US", "en"])
    webgl_vendor: str = "Intel Inc."
    webgl_renderer: str = "Intel Iris Xe Graphics"
    timezone_offset: int = 540  # +0900 minutes (KST)
    device_pixel_ratio: float = 1.0
    chrome_version_hint: str = ""

    def mutate(self, mutation_rate: float = 0.3) -> Fingerprint:
        """Return a new Fingerprint with slight random variations.

        Mutation rate 0.3 = ~30% of fields change per call.
        Variations are tiny — undetectable to humans, detectable to ML models.
        """
        fp = Fingerprint(
            viewport_width=self.viewport_width,
            viewport_height=self.viewport_height,
            languages=list(self.languages),
            webgl_vendor=self.webgl_vendor,
            webgl_renderer=self.webgl_renderer,
            timezone_offset=self.timezone_offset,
            device_pixel_ratio=self.device_pixel_ratio,
            chrome_version_hint=self.chrome_version_hint,
        )

        if random.random() < mutation_rate:
            # Viewport: guaranteed ±1-4 pixels
            fp.viewport_width += random.choice([-3, -2, -1, 1, 2, 3])
            fp.viewport_height += random.choice([-3, -2, -1, 1, 2, 3])

        if random.random() < mutation_rate:
            # Language order swap
            if len(fp.languages) >= 2 and random.random() < 0.5:
                fp.languages[0], fp.languages[1] = fp.languages[1], fp.languages[0]

        if random.random() < mutation_rate:
            # WebGL vendor: minor string variations
            fp.webgl_vendor = random.choice([
                "Intel Inc.", "Intel", "Intel Open Source Technology Center",
                self.webgl_vendor,
            ])

        if random.random() < mutation_rate:
            fp.webgl_renderer = random.choice([
                "Intel Iris Xe Graphics", "Intel(R) Iris(R) Xe Graphics",
                "Intel Iris Plus Graphics",
                self.webgl_renderer,
            ])

        if random.random() < mutation_rate:
            # devicePixelRatio: ±0.25
            fp.device_pixel_ratio = round(
                fp.device_pixel_ratio + random.choice([0, 0, 0, 0.25, -0.25]), 2
            )

        return fp

    def to_page_overrides(self) -> dict:
        """Convert to Playwright page/viewport overrides."""
        return {
            "viewport": {
                "width": self.viewport_width,
                "height": self.viewport_height,
            },
            "device_scale_factor": max(0.5, self.device_pixel_ratio),
        }

    def to_init_script(self) -> str:
        """Generate JS to override navigator properties."""
        lang_json = json.dumps(self.languages)
        return f"""
            Object.defineProperty(navigator, 'webdriver', {{ get: () => false }});
            Object.defineProperty(navigator, 'languages', {{ get: () => {lang_json} }});
            Object.defineProperty(navigator, 'plugins', {{ get: () => [1, 2, 3, 4, 5] }});
            Object.defineProperty(navigator, 'hardwareConcurrency', {{ get: () => {random.choice([4, 8, 12, 16])} }});
        """


class FingerprintPool:
    """Per-domain fingerprint store with evolution tracking."""

    def __init__(self):
        self._pools: dict[str, list[tuple[Fingerprint, int]]] = {}
        self._load()

    def _file(self) -> str:
        os.makedirs(OBS_DIR, exist_ok=True)
        return os.path.join(OBS_DIR, "fingerprints.jsonl")

    def _load(self) -> None:
        fp = self._file()
        if not os.path.isfile(fp):
            return
        try:
            with open(fp, "r") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                        hh = rec["host_hash"]
                        fp = Fingerprint(
                            viewport_width=rec.get("vw", 1366),
                            viewport_height=rec.get("vh", 900),
                            languages=rec.get("lang", ["en-US", "en"]),
                            webgl_vendor=rec.get("wv", "Intel Inc."),
                            webgl_renderer=rec.get("wr", "Intel Iris Xe Graphics"),
                            timezone_offset=rec.get("tz", 540),
                            device_pixel_ratio=rec.get("dpr", 1.0),
                        )
                        if hh not in self._pools:
                            self._pools[hh] = []
                        self._pools[hh].append((fp, rec.get("successes", 0)))
                    except Exception:
                        continue
        except Exception:
            pass

    def _save(self, host_hash: str, fp: Fingerprint, successes: int) -> None:
        try:
            os.makedirs(OBS_DIR, exist_ok=True)
            with open(self._file(), "a") as f:
                json.dump({
                    "host_hash": host_hash,
                    "vw": fp.viewport_width,
                    "vh": fp.viewport_height,
                    "lang": fp.languages,
                    "wv": fp.webgl_vendor,
                    "wr": fp.webgl_renderer,
                    "tz": fp.timezone_offset,
                    "dpr": fp.device_pixel_ratio,
                    "successes": successes,
                    "ts": int(time.time()),
                }, f)
                f.write("\n")
        except Exception:
            pass

    def get(self, url: str) -> Fingerprint:
        """Get the best fingerprint for this domain, or a fresh one.

        Returns a mutated copy — never the exact same fingerprint twice.
        """
        hh = _host_hash(url)
        entries = self._pools.get(hh, [])

        if entries:
            # Pick the most successful fingerprint as base, then mutate
            entries.sort(key=lambda x: x[1], reverse=True)
            base = entries[0][0]
            # Mutation rate scales inversely with success count (explore less on proven FPs)
            successes = entries[0][1]
            rate = max(0.1, 0.3 - (successes * 0.05))
            return base.mutate(mutation_rate=rate)

        return Fingerprint().mutate(mutation_rate=0.3)

    def record(self, url: str, fp: Fingerprint, success: bool) -> None:
        """Record a fingerprint attempt. Successful FPs survive; failed ones lose weight."""
        hh = _host_hash(url)
        if hh not in self._pools:
            self._pools[hh] = []

        # Find existing or add new
        found = False
        for i, (existing, successes) in enumerate(self._pools[hh]):
            if (existing.viewport_width == fp.viewport_width and
                existing.viewport_height == fp.viewport_height and
                existing.languages == fp.languages):
                if success:
                    self._pools[hh][i] = (existing, successes + 1)
                else:
                    new_successes = max(0, successes - 1)
                    if new_successes == 0:
                        self._pools[hh].pop(i)
                    else:
                        self._pools[hh][i] = (existing, new_successes)
                found = True
                break

        if not found and success:
            self._pools[hh].append((fp, 1))

        # Keep pool bounded
        if len(self._pools[hh]) > 20:
            self._pools[hh].sort(key=lambda x: x[1])
            self._pools[hh] = self._pools[hh][-20:]

        if success:
            self._save(hh, fp, 1)
