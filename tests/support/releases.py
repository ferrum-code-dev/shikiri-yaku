"""A release source that serves fixed releases and bytes, and records every download."""

from __future__ import annotations

from dataclasses import dataclass, field

from shikiri_yaku.domain.releases import Asset, Release
from shikiri_yaku.ports import ReleaseSourceError


def release(tag: str, *, draft: bool = False, names: tuple[str, ...] = ()) -> Release:
    """A release with one asset per name; the URLs use the reserved .invalid domain."""
    assets = tuple(Asset(name, f"https://example.invalid/{tag}/{name}") for name in names)
    return Release(tag=tag, draft=draft, assets=assets)


def asset_url(tag: str, name: str) -> str:
    return f"https://example.invalid/{tag}/{name}"


@dataclass
class FakeReleaseSource:
    listing: list[Release] = field(default_factory=list)
    files: dict[str, bytes] = field(default_factory=dict)
    downloads: list[str] = field(default_factory=list)

    def releases(self) -> list[Release]:
        return list(self.listing)

    def download(self, url: str) -> bytes:
        self.downloads.append(url)
        if url not in self.files:
            message = f"no such asset: {url}"
            raise ReleaseSourceError(message)
        return self.files[url]
