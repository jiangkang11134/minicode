"""技能文件发现、加载与管理系统。

提供从项目目录和用户目录中发现、加载、安装和移除 SKILL.md 技能文件的功能。
支持 project（项目级）和 user（用户级）两种作用域。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class SkillSummary:
    """技能摘要信息，包含名称、描述、路径和来源。"""
    name: str
    description: str
    path: str
    source: str


@dataclass(slots=True)
class LoadedSkill(SkillSummary):
    """已加载的技能，包含完整的 SKILL.md 文件内容。"""
    content: str


def extract_description(markdown: str) -> str:
    """从 SKILL.md 内容中提取描述文本。

    遍历 markdown 段落，找到第一个非标题行的纯文本内容作为技能描述。
    移除返回文本中的反引号。

    参数:
        markdown: SKILL.md 文件的完整内容

    返回:
        提取的描述文本，如果未找到则返回 "No description provided."
    """
    normalized = markdown.replace("\r\n", "\n")
    paragraphs = [block.strip() for block in normalized.split("\n\n") if block.strip()]
    for block in paragraphs:
        if block.startswith("#"):
            continue
        for line in [part.strip() for part in block.split("\n")]:
            if line and not line.startswith("#"):
                return line.replace("`", "")
    return "No description provided."


def _home_dir() -> Path:
    """获取当前用户的家目录路径。"""
    return Path.home()


def _skill_roots(cwd: str | Path) -> list[tuple[Path, str]]:
    """获取所有技能根目录及其来源标识。

    按优先级降序排列：项目级 .mini-code/skills → 用户级 .mini-code/skills
    → 兼容项目级 .claude/skills → 兼容用户级 .claude/skills。

    参数:
        cwd: 当前工作目录（用于解析项目级路径）

    返回:
        (根目录路径, 来源标识字符串) 元组列表
    """
    base = Path(cwd)
    home = _home_dir()
    return [
        (base / ".mini-code" / "skills", "project"),
        (home / ".mini-code" / "skills", "user"),
        (base / ".claude" / "skills", "compat_project"),
        (home / ".claude" / "skills", "compat_user"),
    ]


def _list_skill_dirs(root: Path, source: str) -> list[LoadedSkill]:
    """扫描指定根目录下的所有技能。

    遍历根目录的每个子目录，检查是否存在 SKILL.md 文件。
    如果存在则读取其内容并构造 LoadedSkill 对象。

    参数:
        root: 技能根目录路径
        source: 来源标识（如 "project"、"user"）

    返回:
        LoadedSkill 列表，目录不存在或为空时返回空列表
    """
    if not root.exists():
        return []
    results: list[LoadedSkill] = []
    for entry in root.iterdir():
        try:
            if not entry.is_dir():
                continue
        except OSError:
            # Windows: 不可信挂载点、损坏的符号链接等
            continue
        skill_path = entry / "SKILL.md"
        if not skill_path.exists():
            continue
        try:
            content = skill_path.read_text(encoding="utf-8")
        except OSError:
            continue
        results.append(
            LoadedSkill(
                name=entry.name,
                description=extract_description(content),
                path=str(skill_path),
                source=source,
                content=content,
            )
        )
    return results


def discover_skills(cwd: str | Path) -> list[SkillSummary]:
    """发现当前环境下所有可用的技能。

    遍历所有技能根目录，按优先级去重（同名技能只保留优先级最高的）。
    返回简化的 SkillSummary 列表（不含文件内容）。

    参数:
        cwd: 当前工作目录

    返回:
        SkillSummary 列表
    """
    by_name: dict[str, LoadedSkill] = {}
    for root, source in _skill_roots(cwd):
        for skill in _list_skill_dirs(root, source):
            by_name.setdefault(skill.name, skill)
    return [
        SkillSummary(
            name=skill.name,
            description=skill.description,
            path=skill.path,
            source=skill.source,
        )
        for skill in by_name.values()
    ]


def load_skill(cwd: str | Path, name: str) -> LoadedSkill | None:
    """按名称加载指定技能。

    遍历所有技能根目录，查找名为 name 的技能并返回其完整内容。
    返回按优先级顺序第一个匹配到的技能。

    参数:
        cwd: 当前工作目录
        name: 技能名称

    返回:
        LoadedSkill 对象，如果未找到则返回 None
    """
    normalized_name = name.strip()
    if not normalized_name:
        return None
    for root, source in _skill_roots(cwd):
        skill_path = root / normalized_name / "SKILL.md"
        if skill_path.exists():
            content = skill_path.read_text(encoding="utf-8")
            return LoadedSkill(
                name=normalized_name,
                description=extract_description(content),
                path=str(skill_path),
                source=source,
                content=content,
            )
    return None


def _managed_skill_root(scope: str, cwd: str | Path) -> Path:
    """获取托管技能的目标根目录。

    根据作用域返回对应的技能存储目录。

    参数:
        scope: 作用域，"project" 返回项目级目录，其他返回用户级目录
        cwd: 当前工作目录

    返回:
        目标根目录的 Path 对象
    """
    return (Path(cwd) / ".mini-code" / "skills") if scope == "project" else (_home_dir() / ".mini-code" / "skills")


def install_skill(cwd: str | Path, source_path: str, name: str | None = None, scope: str = "user") -> dict[str, str]:
    """安装一个技能到指定作用域。

    从源路径复制 SKILL.md 到目标作用域的技能目录。
    如果 source_path 是目录，自动寻找其下的 SKILL.md；
    如果是文件则直接使用。技能名称可自定义，未指定时从路径推断。

    参数:
        cwd: 当前工作目录
        source_path: 源文件或目录路径（相对或绝对）
        name: 自定义技能名称，未指定则从源路径推断
        scope: 作用域，"project" 或 "user"，默认 "user"

    返回:
        包含技能名称 "name" 和目标路径 "targetPath" 的字典

    抛出:
        RuntimeError: 源路径中未找到 SKILL.md 或技能名称为空
    """
    source = Path(source_path)
    if not source.is_absolute():
        source = Path(cwd) / source
    if source.is_dir():
        skill_file = source / "SKILL.md"
        inferred_name = source.name
    else:
        skill_file = source if source.name == "SKILL.md" else source / "SKILL.md"
        inferred_name = skill_file.parent.name
    if not skill_file.exists():
        raise RuntimeError(f"No SKILL.md found in {source}")

    skill_name = (name or inferred_name).strip()
    if not skill_name:
        raise RuntimeError("Skill name cannot be empty.")

    target_dir = _managed_skill_root(scope, cwd) / skill_name
    target_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(skill_file, target_dir / "SKILL.md")
    return {"name": skill_name, "targetPath": str(target_dir / "SKILL.md")}


def remove_managed_skill(cwd: str | Path, name: str, scope: str = "user") -> dict[str, object]:
    """移除指定作用域中的托管技能。

    删除技能目录及其所有内容。如果技能不存在则返回 removed=False。

    参数:
        cwd: 当前工作目录
        name: 技能名称
        scope: 作用域，"project" 或 "user"，默认 "user"

    返回:
        包含移除结果 "removed"（bool）和目标路径 "targetPath" 的字典
    """
    target_path = _managed_skill_root(scope, cwd) / name
    if not target_path.exists():
        return {"removed": False, "targetPath": str(target_path)}
    shutil.rmtree(target_path)
    return {"removed": True, "targetPath": str(target_path)}
