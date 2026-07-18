from __future__ import annotations

import operator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from pydicom import dcmread


@dataclass(frozen=True, slots=True)
class PdiVolumeWarning:
    code: str
    message: str
    study_instance_uid: str = ""
    total_bytes: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "study_instance_uid": self.study_instance_uid,
            "total_bytes": self.total_bytes,
        }


@dataclass(frozen=True, slots=True)
class PdiVolume:
    number: int
    files: tuple[Path, ...]
    study_instance_uids: tuple[str, ...]
    total_bytes: int
    oversized: bool = False

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def study_count(self) -> int:
        return len(self.study_instance_uids)

    def to_dict(self) -> dict[str, object]:
        return {
            "number": self.number,
            "files": [str(path) for path in self.files],
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "study_count": self.study_count,
            "study_instance_uids": list(self.study_instance_uids),
            "oversized": self.oversized,
        }


@dataclass(frozen=True, slots=True)
class PdiVolumePlan:
    capacity_bytes: int
    volumes: tuple[PdiVolume, ...]
    total_files: int
    total_bytes: int
    total_studies: int
    warnings: tuple[PdiVolumeWarning, ...] = ()

    @property
    def split(self) -> bool:
        return len(self.volumes) > 1

    def to_dict(self) -> dict[str, object]:
        return {
            "capacity_bytes": self.capacity_bytes,
            "total_files": self.total_files,
            "total_bytes": self.total_bytes,
            "total_studies": self.total_studies,
            "split": self.split,
            "volumes": [volume.to_dict() for volume in self.volumes],
            "warnings": [warning.to_dict() for warning in self.warnings],
        }


@dataclass(slots=True)
class _StudyGroup:
    key: str
    study_instance_uid: str
    files: list[Path] = field(default_factory=list)
    total_bytes: int = 0


def plan_pdi_volumes(
    files: Iterable[str | Path],
    capacity_bytes: int = 0,
    *,
    fixed_volume_overhead_bytes: int = 0,
    per_file_overhead_bytes: int = 0,
    cancel_check: Callable[[], None] | None = None,
) -> PdiVolumePlan:
    """Plan deterministic PDI volumes without ever splitting a Study.

    Study groups and files retain their first appearance order.  A capacity of
    zero disables splitting.  A Study larger than a positive capacity receives
    a dedicated oversized volume and an explicit warning.
    """

    try:
        capacity = operator.index(capacity_bytes)
    except TypeError as exc:
        raise TypeError("PDI 分卷容量必须是整数") from exc
    if capacity < 0:
        raise ValueError("PDI 分卷容量不能为负数")
    try:
        fixed_overhead = operator.index(fixed_volume_overhead_bytes)
        per_file_overhead = operator.index(per_file_overhead_bytes)
    except TypeError as exc:
        raise TypeError("PDI 分卷开销必须是整数") from exc
    if fixed_overhead < 0 or per_file_overhead < 0:
        raise ValueError("PDI 分卷开销不能为负数")

    groups: dict[str, _StudyGroup] = {}
    ordered_groups: list[_StudyGroup] = []
    warnings: list[PdiVolumeWarning] = []
    total_files = 0
    total_bytes = 0

    for position, value in enumerate(files, start=1):
        if cancel_check is not None:
            cancel_check()
        path = Path(value).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"DICOM 文件不存在：{path}")
        try:
            resolved = path.resolve(strict=True)
            size = resolved.stat().st_size
            dataset = dcmread(
                resolved,
                stop_before_pixels=True,
                specific_tags=["StudyInstanceUID"],
            )
        except FileNotFoundError:
            raise FileNotFoundError(f"DICOM 文件不存在：{path}") from None
        except (OSError, ValueError) as exc:
            raise ValueError(f"无法读取 DICOM 文件 {path}：{exc}") from exc
        except Exception as exc:
            raise ValueError(f"无法读取 DICOM 文件 {path}：{exc}") from exc
        if cancel_check is not None:
            cancel_check()

        study_uid = str(dataset.get("StudyInstanceUID", "") or "").strip()
        if study_uid:
            group_key = f"study:{study_uid}"
            display_uid = study_uid
        else:
            # Without a Study UID there is no safe basis for merging files.
            # Keep each such object whole and independent, matching the current
            # PDI exporter's conservative per-file fallback behavior.
            group_key = f"missing:{position}:{resolved}"
            display_uid = f"MISSING-STUDY-{position:06d}"
            warnings.append(
                PdiVolumeWarning(
                    code="missing_study_instance_uid",
                    message=(
                        f"文件缺少 Study Instance UID，已作为独立 Study 规划：{resolved.name}"
                    ),
                    study_instance_uid=display_uid,
                    total_bytes=size,
                )
            )

        group = groups.get(group_key)
        if group is None:
            group = _StudyGroup(group_key, display_uid)
            groups[group_key] = group
            ordered_groups.append(group)
        group.files.append(resolved)
        group.total_bytes += size
        total_files += 1
        total_bytes += size

    if not ordered_groups:
        return PdiVolumePlan(
            capacity_bytes=capacity,
            volumes=(),
            total_files=0,
            total_bytes=0,
            total_studies=0,
        )

    volume_groups: list[list[_StudyGroup]] = []
    oversized_numbers: set[int] = set()
    if capacity == 0:
        volume_groups.append(ordered_groups)
    else:
        current: list[_StudyGroup] = []
        current_estimated_bytes = fixed_overhead
        for group in ordered_groups:
            if cancel_check is not None:
                cancel_check()
            group_estimated_bytes = (
                group.total_bytes + len(group.files) * per_file_overhead
            )
            if fixed_overhead + group_estimated_bytes > capacity:
                if current:
                    volume_groups.append(current)
                    current = []
                    current_estimated_bytes = fixed_overhead
                volume_groups.append([group])
                oversized_numbers.add(len(volume_groups))
                warnings.append(
                    PdiVolumeWarning(
                        code="study_exceeds_capacity",
                        message=(
                            f"Study {group.study_instance_uid} 大于单卷容量，"
                            "已独占一卷且不会拆分"
                        ),
                        study_instance_uid=group.study_instance_uid,
                        total_bytes=group.total_bytes,
                    )
                )
                continue
            if (
                current
                and current_estimated_bytes + group_estimated_bytes > capacity
            ):
                volume_groups.append(current)
                current = []
                current_estimated_bytes = fixed_overhead
            current.append(group)
            current_estimated_bytes += group_estimated_bytes
        if current:
            volume_groups.append(current)

    volumes = tuple(
        _make_volume(
            number,
            grouped_studies,
            oversized=number in oversized_numbers,
        )
        for number, grouped_studies in enumerate(volume_groups, start=1)
    )
    return PdiVolumePlan(
        capacity_bytes=capacity,
        volumes=volumes,
        total_files=total_files,
        total_bytes=total_bytes,
        total_studies=len(ordered_groups),
        warnings=tuple(warnings),
    )


def _make_volume(
    number: int, groups: list[_StudyGroup], *, oversized: bool
) -> PdiVolume:
    return PdiVolume(
        number=number,
        files=tuple(path for group in groups for path in group.files),
        study_instance_uids=tuple(group.study_instance_uid for group in groups),
        total_bytes=sum(group.total_bytes for group in groups),
        oversized=oversized,
    )
