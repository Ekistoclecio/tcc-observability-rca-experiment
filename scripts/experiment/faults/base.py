from __future__ import annotations

from abc import ABC, abstractmethod


class FaultInjector(ABC):
    """Lifecycle contract shared by all controlled fault mechanisms."""

    @abstractmethod
    def prepare(self) -> dict: ...

    @abstractmethod
    def apply(self) -> dict: ...

    @abstractmethod
    def confirm_active(self, timeout: float) -> dict: ...

    @abstractmethod
    def recover(self) -> dict: ...

    @abstractmethod
    def confirm_recovered(self, timeout: float) -> dict: ...

    @abstractmethod
    def cleanup(self) -> dict: ...
