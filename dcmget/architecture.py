from __future__ import annotations

import struct
import sys
from pathlib import Path


IMAGE_FILE_MACHINE_AMD64 = 0x8664
IMAGE_FILE_MACHINE_ARM64 = 0xAA64
IMAGE_FILE_MACHINE_I386 = 0x014C

_MACHINE_NAMES = {
    IMAGE_FILE_MACHINE_AMD64: "AMD64/x64",
    IMAGE_FILE_MACHINE_ARM64: "ARM64",
    IMAGE_FILE_MACHINE_I386: "x86/32-bit",
}


class ArchitectureError(RuntimeError):
    """Raised when DcmGet is started or built with an unsupported architecture."""


def pe_machine(path: str | Path) -> int:
    """Return the COFF machine field from a Windows PE executable."""

    executable = Path(path)
    try:
        with executable.open("rb") as handle:
            dos_header = handle.read(64)
            if len(dos_header) != 64 or dos_header[:2] != b"MZ":
                raise ArchitectureError(f"不是有效的 Windows PE 文件：{executable}")
            pe_offset = struct.unpack_from("<I", dos_header, 0x3C)[0]
            if pe_offset < 64:
                raise ArchitectureError(f"Windows PE 头偏移无效：{executable}")
            handle.seek(pe_offset)
            coff_header = handle.read(6)
    except ArchitectureError:
        raise
    except (OSError, OverflowError, struct.error) as exc:
        raise ArchitectureError(
            f"无法读取 Windows PE 架构：{executable}：{exc}"
        ) from exc

    if len(coff_header) != 6 or coff_header[:4] != b"PE\0\0":
        raise ArchitectureError(f"Windows PE 头无效：{executable}")
    return struct.unpack_from("<H", coff_header, 4)[0]


def require_amd64_pe(
    path: str | Path,
    description: str = "Windows 可执行文件",
) -> None:
    """Require an AMD64/x64 PE, including when the host OS is Windows ARM64."""

    machine = pe_machine(path)
    if machine != IMAGE_FILE_MACHINE_AMD64:
        name = _MACHINE_NAMES.get(machine, f"未知架构 0x{machine:04X}")
        raise ArchitectureError(f"{description} 必须是 AMD64/x64，当前为 {name}：{path}")


def ensure_supported_runtime(
    *,
    platform_name: str | None = None,
    executable: str | Path | None = None,
    pointer_bits: int | None = None,
) -> None:
    """Reject every 32-bit runtime and require an x64 process on Windows.

    Reading the executable PE header, instead of the host CPU name, deliberately
    permits an AMD64 build running through the Windows ARM64 x64 compatibility
    layer while rejecting native ARM64 and x86 Python runtimes.
    """

    bits = pointer_bits if pointer_bits is not None else struct.calcsize("P") * 8
    if bits != 64:
        raise ArchitectureError(
            f"DcmGet 仅支持 64 位系统和运行时，当前 Python 为 {bits} 位。"
        )

    current_platform = platform_name if platform_name is not None else sys.platform
    if current_platform != "win32":
        return

    require_amd64_pe(
        executable if executable is not None else sys.executable,
        "当前 Windows Python/程序",
    )
