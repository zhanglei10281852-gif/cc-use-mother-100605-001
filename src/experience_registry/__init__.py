"""装备厂经验知识库：条目版本化、复核生效、相似合并建议与完整链路追溯。"""
from .cli import main
from .service import RegistryService

__all__ = ["RegistryService", "main"]
__version__ = "0.1.0"
