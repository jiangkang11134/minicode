"""能力注册表（Capability Registry）—— 自描述的工具注册系统。

受 Skill 架构启发：每个能力（capability）都是自描述、自注册的单元，
包含依赖关系图。工具不再是被隔离的函数，而是可触发、可读取、
可扩展的能力单元，支持按领域、标签的索引和语义搜索。
"""

from __future__ import annotations

import inspect
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Protocol, runtime_checkable

from minicode.logging_config import get_logger

logger = get_logger("capability_registry")


class CapabilityDomain(str, Enum):
    """能力领域的枚举分类。

    用于将注册的能力按功能领域归类，便于按类型检索和
    权限隔离。每个能力属于且仅属于一个领域。
    """
    FILE = "file"
    CODE = "code"
    SEARCH = "search"
    WEB = "web"
    SYSTEM = "system"
    MEMORY = "memory"
    COMMUNICATION = "communication"
    ANALYSIS = "analysis"
    EXECUTION = "execution"
    UNKNOWN = "unknown"


class CapabilityScope(str, Enum):
    """能力作用域的枚举分类。

    定义能力的副作用范围，用于权限决策和审计：
    - READONLY: 只读操作，无副作用
    - WRITE: 写入操作，修改现有状态
    - DESTRUCTIVE: 破坏性操作，可能造成不可逆影响
    - EXTERNAL: 外部资源操作（网络、进程等）
    """
    READONLY = "readonly"
    WRITE = "write"
    DESTRUCTIVE = "destructive"
    EXTERNAL = "external"


@dataclass
class CapabilityMetadata:
    """能力元数据，描述一个能力的基本信息和约束。

    包含名称、领域、作用域、版本、作者、依赖列表、
    所需权限、示例用法和标签等，用于能力发现和权限评估。
    """
    name: str
    domain: CapabilityDomain
    scope: CapabilityScope
    description: str
    version: str = "1.0.0"
    author: str = ""
    dependencies: list[str] = field(default_factory=list)
    required_permissions: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """将元数据序列化为字典。

        返回:
            包含所有字段的字典（枚举值已转换为字符串）
        """
        return {
            "name": self.name, "domain": self.domain.value,
            "scope": self.scope.value, "description": self.description,
            "version": self.version, "author": self.author,
            "dependencies": self.dependencies,
            "required_permissions": self.required_permissions,
            "examples": self.examples, "tags": self.tags,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }


@runtime_checkable
class Capability(Protocol):
    """能力协议（Protocol），定义能力必须实现的接口。

    任何实现了 metadata 属性、execute 方法和 validate 方法的
    对象均可被视为 Capability，支持 isinstance 运行时检查。
    """

    @property
    def metadata(self) -> CapabilityMetadata:
        """获取能力元数据。 """
        ...

    def execute(self, params: dict[str, Any]) -> dict[str, Any]:
        """执行能力逻辑。

        参数:
            params: 执行参数

        返回:
            执行结果字典
        """
        ...

    def validate(self, params: dict[str, Any]) -> tuple[bool, str]:
        """验证参数是否合法。

        参数:
            params: 待验证的参数

        返回:
            (是否合法, 错误消息) 元组
        """
        ...


@dataclass
class RegisteredCapability:
    """已注册的能力封装，包含元数据、处理函数和执行统计。

    除了持有原始的处理函数和验证器外，还会自动记录
    调用次数、总执行时间和最后使用时间，便于监控和审计。
    """
    metadata: CapabilityMetadata
    handler: Callable[..., Any]
    validator: Callable[[dict[str, Any]], tuple[bool, str]] | None = None
    instance: Any | None = None
    call_count: int = 0
    total_execution_time: float = 0.0
    last_used: float = 0.0

    def execute(self, params: dict[str, Any]) -> Any:
        """执行能力处理函数，并自动记录调用统计。

        如果能力是从实例方法注册的，自动传入 self 参数。
        执行时间会被累加到 total_execution_time 中，异常时
        也会先记录已消耗的时间再抛出。

        参数:
            params: 执行参数字典

        返回:
            处理函数的返回结果
        """
        start = time.time()
        self.call_count += 1
        self.last_used = start
        try:
            if self.instance is not None:
                result = self.handler(self.instance, **params)
            else:
                result = self.handler(**params)
            self.total_execution_time += time.time() - start
            return result
        except Exception:
            self.total_execution_time += time.time() - start
            raise

    def validate(self, params: dict[str, Any]) -> tuple[bool, str]:
        """验证参数是否满足能力的要求。

        优先使用注册时的验证器；无验证器时返回 (True, "")。

        参数:
            params: 待验证的参数字典

        返回:
            (是否合法, 错误消息) 元组
        """
        if self.validator:
            return self.validator(params)
        return True, ""

    @property
    def avg_execution_time(self) -> float:
        """计算平均单次执行时间（秒）。

        返回:
            平均执行时长，单位秒；未执行过返回 0.0
        """
        return self.total_execution_time / self.call_count if self.call_count else 0.0

    def to_dict(self) -> dict[str, Any]:
        """将注册能力信息序列化为字典。

        返回:
            包含元数据、调用次数、平均执行时间和最后使用时间的字典
        """
        return {
            "metadata": self.metadata.to_dict(),
            "call_count": self.call_count,
            "avg_execution_time_ms": round(self.avg_execution_time * 1000, 2),
            "last_used": self.last_used,
        }


class CapabilityRegistry:
    """能力注册表，管理所有 Capability 的注册、检索和查询。

    提供按名称、领域、标签的索引，支持语义搜索、
    依赖关系检查，以及注册能力的统计概览。
    """

    def __init__(self):
        """初始化空的注册表，创建名称索引、领域索引、标签索引和依赖图。 """
        self._capabilities: dict[str, RegisteredCapability] = {}
        self._domain_index: dict[CapabilityDomain, set[str]] = {}
        self._tag_index: dict[str, set[str]] = {}
        self._dependency_graph: dict[str, set[str]] = {}

    def register(
        self,
        metadata: CapabilityMetadata,
        handler: Callable[..., Any],
        validator: Callable | None = None,
        instance: Any | None = None,
    ) -> RegisteredCapability:
        """注册一个新能力或更新已有能力。

        如果同名的能力已存在，会用新信息覆盖并记录警告。
        同时更新领域索引、标签索引和依赖关系图。

        参数:
            metadata: 能力的元数据
            handler: 能力处理函数
            validator: 可选参数验证函数
            instance: 可选实例对象（用于实例方法注册）

        返回:
            注册后的 RegisteredCapability 对象
        """
        name = metadata.name
        if name in self._capabilities:
            logger.warning("Capability '%s' already registered, updating", name)

        cap = RegisteredCapability(metadata=metadata, handler=handler, validator=validator, instance=instance)
        self._capabilities[name] = cap

        domain = metadata.domain
        if domain not in self._domain_index:
            self._domain_index[domain] = set()
        self._domain_index[domain].add(name)

        for tag in metadata.tags:
            if tag not in self._tag_index:
                self._tag_index[tag] = set()
            self._tag_index[tag].add(name)

        self._dependency_graph[name] = set(metadata.dependencies)
        logger.debug("Registered capability: %s (%s)", name, domain.value)
        return cap

    def unregister(self, name: str) -> bool:
        """从注册表中移除一个能力。

        同时清理领域索引、标签索引和依赖关系图中的记录。

        参数:
            name: 要移除的能力名称

        返回:
            成功移除返回 True，能力不存在返回 False
        """
        if name not in self._capabilities:
            return False
        cap = self._capabilities.pop(name)
        self._domain_index.get(cap.metadata.domain, set()).discard(name)
        for tag in cap.metadata.tags:
            self._tag_index.get(tag, set()).discard(name)
        self._dependency_graph.pop(name, None)
        logger.debug("Unregistered capability: %s", name)
        return True

    def get(self, name: str) -> RegisteredCapability | None:
        """按名称获取已注册的能力。

        参数:
            name: 能力名称

        返回:
            RegisteredCapability 对象，不存在则返回 None
        """
        return self._capabilities.get(name)

    def has(self, name: str) -> bool:
        """检查指定名称的能力是否已注册。

        参数:
            name: 能力名称

        返回:
            存在返回 True，否则返回 False
        """
        return name in self._capabilities

    def list_all(self) -> list[str]:
        """列出所有已注册能力的名称。

        返回:
            能力名称列表
        """
        return list(self._capabilities.keys())

    def list_by_domain(self, domain: CapabilityDomain) -> list[str]:
        """按领域列出该领域下所有能力的名称。

        参数:
            domain: 目标领域

        返回:
            属于该领域的能力名称列表
        """
        return list(self._domain_index.get(domain, set()))

    def list_by_tag(self, tag: str) -> list[str]:
        """按标签列出所有附加了该标签的能力名称。

        参数:
            tag: 目标标签

        返回:
            具有该标签的能力名称列表
        """
        return list(self._tag_index.get(tag, set()))

    def search(self, query: str) -> list[tuple[str, float]]:
        """对能力进行语义搜索，返回按相关性评分排序的结果。

        评分规则：
        - 名称匹配查询：+1.0
        - 描述匹配查询：+0.5
        - 标签匹配查询：+0.3
        - 领域匹配查询：+0.2

        参数:
            query: 搜索关键词

        返回:
            (能力名称, 相关度评分) 元组列表，按评分降序排列
        """
        query_lower = query.lower()
        results: list[tuple[str, float]] = []
        for name, cap in self._capabilities.items():
            score = 0.0
            if query_lower in name.lower():
                score += 1.0
            if query_lower in cap.metadata.description.lower():
                score += 0.5
            for tag in cap.metadata.tags:
                if query_lower in tag.lower():
                    score += 0.3
            if query_lower in cap.metadata.domain.value.lower():
                score += 0.2
            if score > 0:
                results.append((name, score))
        results.sort(key=lambda x: x[1], reverse=True)
        return results

    def get_dependencies(self, name: str) -> set[str]:
        """获取指定能力的直接依赖集合。

        参数:
            name: 能力名称

        返回:
            直接依赖的能力名称集合（浅层复制）
        """
        return self._dependency_graph.get(name, set()).copy()

    def get_all_dependencies(self, name: str) -> set[str]:
        """递归获取指定能力的所有传递依赖。

        使用深度优先搜索遍历依赖关系图，排除自身。

        参数:
            name: 能力名称

        返回:
            所有传递依赖的能力名称集合
        """
        visited: set[str] = set()
        stack = [name]
        while stack:
            current = stack.pop()
            if current in visited:
                continue
            visited.add(current)
            for dep in self._dependency_graph.get(current, set()):
                if dep not in visited:
                    stack.append(dep)
        visited.discard(name)
        return visited

    def check_dependencies(self, name: str) -> tuple[bool, list[str]]:
        """检查指定能力的依赖是否全部已注册。

        参数:
            name: 能力名称

        返回:
            (是否满足, 缺失依赖名称列表) 元组
        """
        deps = self._dependency_graph.get(name, set())
        missing = [d for d in deps if d not in self._capabilities]
        return len(missing) == 0, missing

    def get_stats(self) -> dict[str, Any]:
        """获取注册表的统计概览信息。

        包含总能力数、各领域能力数、各标签能力数以及
        使用最频繁的前 10 个能力。

        返回:
            统计信息字典
        """
        return {
            "total_capabilities": len(self._capabilities),
            "domains": {domain.value: len(caps) for domain, caps in self._domain_index.items()},
            "tags": {tag: len(caps) for tag, caps in self._tag_index.items()},
            "most_used": sorted(
                [(name, cap.call_count) for name, cap in self._capabilities.items()],
                key=lambda x: x[1], reverse=True,
            )[:10],
        }

    def to_dict(self) -> dict[str, Any]:
        """将整个注册表序列化为字典。

        返回:
            包含所有能力详情和统计信息的完整字典
        """
        return {
            "capabilities": {name: cap.to_dict() for name, cap in self._capabilities.items()},
            "stats": self.get_stats(),
        }


_registry: CapabilityRegistry | None = None


def get_registry() -> CapabilityRegistry:
    """获取全局能力注册表单例。

    首次调用时惰性初始化 CapabilityRegistry 实例。

    返回:
        CapabilityRegistry 单例对象
    """
    global _registry
    if _registry is None:
        _registry = CapabilityRegistry()
    return _registry


def capability(
    name: str,
    domain: CapabilityDomain,
    scope: CapabilityScope,
    description: str,
    version: str = "1.0.0",
    dependencies: list[str] | None = None,
    permissions: list[str] | None = None,
    tags: list[str] | None = None,
    examples: list[str] | None = None,
):
    """将函数装饰为 Capability 并自动注册到全局注册表。

    这是一个装饰器工厂，基于被装饰函数的签名自动生成参数验证器，
    检查必填参数是否完整，然后调用 get_registry().register() 注册。

    参数:
        name: 能力名称
        domain: 能力所属领域
        scope: 能力作用域
        description: 能力描述
        version: 版本号，默认为 "1.0.0"
        dependencies: 依赖的能力名称列表
        permissions: 所需权限列表
        tags: 标签列表
        examples: 使用示例列表
    """
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        metadata = CapabilityMetadata(
            name=name, domain=domain, scope=scope, description=description,
            version=version, dependencies=dependencies or [],
            required_permissions=permissions or [], tags=tags or [], examples=examples or [],
        )
        sig = inspect.signature(func)

        def validator(params: dict[str, Any]) -> tuple[bool, str]:
            """根据函数签名验证参数字典。

            检查必填参数（无默认值的参数）是否全部提供，跳过 self。

            参数:
                params: 待验证的参数字典

            返回:
                (是否合法, 错误消息) 元组
            """
            for param_name, param in sig.parameters.items():
                if param_name == "self":
                    continue
                if param.default is inspect.Parameter.empty and param_name not in params:
                    return False, f"Missing required parameter: {param_name}"
            return True, ""

        get_registry().register(metadata, func, validator)
        return func
    return decorator


def register_instance_capability(
    instance: Any,
    method_name: str,
    metadata: CapabilityMetadata,
) -> RegisteredCapability:
    """将实例的方法注册为能力。

    绑定实例到 RegisteredCapability，使得 execute 时
    自动将 instance 作为第一个参数传给 handler。

    参数:
        instance: 拥有该方法的实例对象
        method_name: 方法名称
        metadata: 能力元数据

    返回:
        注册后的 RegisteredCapability 对象
    """
    handler = getattr(instance, method_name)
    return get_registry().register(metadata, handler, instance=instance)
