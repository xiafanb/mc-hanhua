from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from .models import TextUnit


class ExtractorPlugin(ABC):
    name: str

    @abstractmethod
    def supports(self, path: Path) -> bool:
        raise NotImplementedError

    @abstractmethod
    def extract(self, root: Path, path: Path) -> list[TextUnit]:
        raise NotImplementedError


class WriterPlugin(ABC):
    name: str

    @abstractmethod
    def supports(self, path: Path) -> bool:
        raise NotImplementedError

    @abstractmethod
    def write(self, root: Path, path: Path, units: list[TextUnit]) -> int:
        raise NotImplementedError


class PluginRegistry:
    def __init__(self) -> None:
        self.extractors: list[ExtractorPlugin] = []
        self.writers: list[WriterPlugin] = []

    def add_extractor(self, plugin: ExtractorPlugin) -> None:
        self.extractors.append(plugin)

    def add_writer(self, plugin: WriterPlugin) -> None:
        self.writers.append(plugin)

    def extractor_for(self, path: Path) -> ExtractorPlugin | None:
        for plugin in self.extractors:
            if plugin.supports(path):
                return plugin
        return None

    def writer_for(self, path: Path) -> WriterPlugin | None:
        for plugin in self.writers:
            if plugin.supports(path):
                return plugin
        return None
