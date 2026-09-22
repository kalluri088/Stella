"""Minimal provider-agnostic tool abstractions."""

import datetime as dt
import http.client
import ipaddress
import os
import platform
import socket
import ssl
import time
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PureWindowsPath
from urllib.parse import SplitResult, urlsplit


@dataclass(frozen=True)
class ToolResult:
    """The result of executing a tool."""

    success: bool
    output: str


class RiskLevel(str, Enum):
    """Trusted application classification for a tool action."""

    SAFE = "safe"
    SENSITIVE = "sensitive"
    DANGEROUS = "dangerous"


@dataclass(frozen=True)
class ApprovalRequest:
    """The exact capability and arguments a trusted application may approve."""

    capability: str
    arguments: dict[str, object]


@dataclass(frozen=True)
class ToolApproval:
    """An application-produced approval for one exact tool action."""

    request: ApprovalRequest
    approved: bool


@dataclass(frozen=True)
class AuditRecord:
    """A trusted in-memory record of one tool dispatch attempt."""

    capability: str | None
    arguments: dict[str, object]
    risk_level: RiskLevel | None
    approval_required: bool
    approval_granted: bool | None
    execution_success: bool
    timestamp: str


class Tool(ABC):
    """Interface for tools that execute structured arguments."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Return the tool's name."""

    @property
    @abstractmethod
    def description(self) -> str:
        """Return the tool's description."""

    @property
    def argument_schema(self) -> dict[str, object]:
        """Describe the tool's accepted arguments for the decision prompt."""

        return {}

    @property
    def risk_level(self) -> RiskLevel:
        """Return the trusted baseline risk classification."""

        return RiskLevel.SAFE

    @abstractmethod
    def validate_arguments(self, arguments: dict[str, object]) -> bool:
        """Return whether arguments satisfy this tool's input contract."""

    @abstractmethod
    def execute(self, arguments: dict[str, object]) -> ToolResult:
        """Execute the tool with structured arguments."""


class EchoTool(Tool):
    """Deterministic example tool for testing."""

    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "Returns the supplied message."

    @property
    def argument_schema(self) -> dict[str, object]:
        return {"message": "string"}

    def validate_arguments(self, arguments: dict[str, object]) -> bool:
        return (
            isinstance(arguments, dict)
            and set(arguments) == {"message"}
            and isinstance(arguments["message"], str)
        )

    def execute(self, arguments: dict[str, object]) -> ToolResult:
        if not self.validate_arguments(arguments):
            return ToolResult(success=False, output="Invalid tool arguments.")
        return ToolResult(success=True, output=str(arguments["message"]))


class SystemInfoTool(Tool):
    """Read a small, non-sensitive subset of local system information."""

    @property
    def name(self) -> str:
        return "system_info"

    @property
    def description(self) -> str:
        return "Reads the current hostname, platform, or CPU information."

    @property
    def argument_schema(self) -> dict[str, object]:
        return {"kind": "hostname|platform|cpu"}

    def validate_arguments(self, arguments: dict[str, object]) -> bool:
        return (
            isinstance(arguments, dict)
            and set(arguments) == {"kind"}
            and arguments["kind"] in {"hostname", "platform", "cpu"}
        )

    def execute(self, arguments: dict[str, object]) -> ToolResult:
        if not self.validate_arguments(arguments):
            return ToolResult(success=False, output="Invalid tool arguments.")

        try:
            kind = arguments["kind"]
            if kind == "hostname":
                output = platform.node()
            elif kind == "platform":
                output = platform.platform()
            else:
                output = (
                    f"Processor: {platform.processor() or 'unknown'}; "
                    f"CPU count: {os.cpu_count() or 'unknown'}"
                )
            return ToolResult(success=True, output=output or "unknown")
        except Exception:  # noqa: BLE001 - read-only tool failures are contained
            return ToolResult(
                success=False, output="System information unavailable."
            )


class DateTimeTool(Tool):
    """Read the host's current local date and time."""

    @property
    def name(self) -> str:
        return "datetime"

    @property
    def description(self) -> str:
        return "Reads the current local date, time, datetime, or weekday."

    @property
    def argument_schema(self) -> dict[str, object]:
        return {"kind": "date|time|datetime|weekday"}

    def validate_arguments(self, arguments: dict[str, object]) -> bool:
        return (
            isinstance(arguments, dict)
            and set(arguments) == {"kind"}
            and arguments["kind"] in {"date", "time", "datetime", "weekday"}
        )

    def execute(self, arguments: dict[str, object]) -> ToolResult:
        if not self.validate_arguments(arguments):
            return ToolResult(success=False, output="Invalid tool arguments.")

        try:
            current = dt.datetime.now().astimezone()
            kind = arguments["kind"]
            if kind == "date":
                output = current.date().isoformat()
            elif kind == "time":
                output = current.strftime("%H:%M:%S %z")
            elif kind == "datetime":
                output = current.isoformat(timespec="seconds")
            else:
                output = current.strftime("%A")
            return ToolResult(success=True, output=output)
        except Exception:  # noqa: BLE001 - read-only tool failures are contained
            return ToolResult(success=False, output="Date/time unavailable.")


class FileSystemReadTool(Tool):
    """Read bounded UTF-8 text files from one configured workspace."""

    MAX_FILE_SIZE = 1_048_576

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).resolve()

    @property
    def name(self) -> str:
        return "filesystem_read"

    @property
    def description(self) -> str:
        return (
            "Reads a UTF-8 text file below the configured Stella workspace. "
            "Requires a relative path and cannot access arbitrary locations."
        )

    @property
    def argument_schema(self) -> dict[str, object]:
        return {"path": "relative UTF-8 text-file path"}

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.SENSITIVE

    def validate_arguments(self, arguments: dict[str, object]) -> bool:
        if (
            not isinstance(arguments, dict)
            or set(arguments) != {"path"}
            or not isinstance(arguments["path"], str)
        ):
            return False

        path = arguments["path"]
        return (
            bool(path.strip())
            and "\x00" not in path
            and self._is_relative(path)
        )

    @staticmethod
    def _is_relative(path: str) -> bool:
        candidate = Path(path)
        windows_candidate = PureWindowsPath(path)
        return (
            not candidate.is_absolute()
            and not windows_candidate.is_absolute()
            and not windows_candidate.drive
            and ".." not in candidate.parts
        )

    def _resolve_in_workspace(self, path: str) -> Path | None:
        try:
            resolved = (self.workspace / path).resolve()
            resolved.relative_to(self.workspace)
        except (OSError, RuntimeError, ValueError):
            return None
        return resolved

    def execute(self, arguments: dict[str, object]) -> ToolResult:
        if not self.validate_arguments(arguments):
            return ToolResult(success=False, output="Invalid tool arguments.")

        path = arguments["path"]
        resolved = self._resolve_in_workspace(path)
        if resolved is None:
            return ToolResult(success=False, output="File is outside workspace.")

        try:
            if not resolved.exists():
                return ToolResult(success=False, output="File was not found.")
            if not resolved.is_file():
                return ToolResult(success=False, output="File is not a regular file.")
            if resolved.stat().st_size > self.MAX_FILE_SIZE:
                return ToolResult(success=False, output="File is too large.")
            return ToolResult(
                success=True,
                output=resolved.read_text(encoding="utf-8"),
            )
        except FileNotFoundError:
            return ToolResult(success=False, output="File was not found.")
        except UnicodeDecodeError:
            return ToolResult(success=False, output="File is not valid UTF-8.")
        except (OSError, RuntimeError):
            return ToolResult(success=False, output="File could not be read.")


class FileSystemWriteTool(FileSystemReadTool):
    """Create bounded UTF-8 text files inside one configured workspace."""

    @property
    def name(self) -> str:
        return "filesystem_write"

    @property
    def description(self) -> str:
        return (
            "Creates a new UTF-8 text file below the configured Stella "
            "workspace. Requires a relative path and explicit approval."
        )

    @property
    def argument_schema(self) -> dict[str, object]:
        return {
            "path": "relative UTF-8 text-file path",
            "content": "UTF-8 string",
        }

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.DANGEROUS

    def validate_arguments(self, arguments: dict[str, object]) -> bool:
        if (
            not isinstance(arguments, dict)
            or set(arguments) != {"path", "content"}
            or not isinstance(arguments["path"], str)
            or not isinstance(arguments["content"], str)
        ):
            return False

        path = arguments["path"]
        content = arguments["content"]
        try:
            content_size = len(content.encode("utf-8"))
        except UnicodeEncodeError:
            return False
        return (
            bool(path.strip())
            and "\x00" not in path
            and self._is_relative(path)
            and content_size <= self.MAX_FILE_SIZE
        )

    def execute(self, arguments: dict[str, object]) -> ToolResult:
        if not self.validate_arguments(arguments):
            return ToolResult(success=False, output="Invalid tool arguments.")

        path = arguments["path"]
        content = arguments["content"]
        resolved = self._resolve_in_workspace(path)
        if resolved is None:
            return ToolResult(success=False, output="File is outside workspace.")

        candidate = self.workspace / path
        try:
            if not self.workspace.is_dir():
                return ToolResult(success=False, output="Workspace unavailable.")
            if os.path.lexists(candidate):
                return ToolResult(success=False, output="File already exists.")
            if not resolved.parent.is_dir():
                return ToolResult(
                    success=False, output="Parent directory unavailable."
                )

            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(resolved, flags, 0o600)
            with os.fdopen(descriptor, "wb") as file:
                file.write(content.encode("utf-8"))
            return ToolResult(success=True, output="File created.")
        except FileExistsError:
            return ToolResult(success=False, output="File already exists.")
        except (OSError, RuntimeError, UnicodeEncodeError):
            return ToolResult(success=False, output="File could not be created.")


class FileSystemDeleteTool(FileSystemReadTool):
    """Delete one existing regular file inside the configured workspace."""

    @property
    def name(self) -> str:
        return "filesystem_delete"

    @property
    def description(self) -> str:
        return (
            "Deletes one existing regular file below the configured Stella "
            "workspace. Requires a relative path and explicit approval."
        )

    @property
    def argument_schema(self) -> dict[str, object]:
        return {"path": "relative UTF-8 text-file path"}

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.DANGEROUS

    def validate_arguments(self, arguments: dict[str, object]) -> bool:
        if (
            not isinstance(arguments, dict)
            or set(arguments) != {"path"}
            or not isinstance(arguments["path"], str)
        ):
            return False

        path = arguments["path"]
        return (
            bool(path.strip())
            and "\x00" not in path
            and not any(character in path for character in "*?[")
            and self._is_relative(path)
        )

    def execute(self, arguments: dict[str, object]) -> ToolResult:
        if not self.validate_arguments(arguments):
            return ToolResult(success=False, output="Invalid tool arguments.")

        path = arguments["path"]
        resolved = self._resolve_in_workspace(path)
        if resolved is None:
            return ToolResult(success=False, output="File is outside workspace.")

        candidate = self.workspace / path
        try:
            if not self.workspace.is_dir():
                return ToolResult(success=False, output="Workspace unavailable.")
            if candidate.is_symlink():
                return ToolResult(
                    success=False, output="Symbolic links are not supported."
                )
            if not resolved.exists():
                return ToolResult(success=False, output="File was not found.")
            if not resolved.is_file():
                return ToolResult(
                    success=False, output="File is not a regular file."
                )
            resolved.unlink()
            return ToolResult(success=True, output="File deleted.")
        except FileNotFoundError:
            return ToolResult(success=False, output="File was not found.")
        except IsADirectoryError:
            return ToolResult(
                success=False, output="File is not a regular file."
            )
        except (OSError, RuntimeError):
            return ToolResult(success=False, output="File could not be deleted.")


class _ValidatedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection pinned to an already-validated numeric address."""

    def __init__(self, hostname: str, address: str, timeout: float) -> None:
        super().__init__(
            hostname,
            port=443,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        self._validated_address = address

    def _create_connection(
        self,
        address: tuple[str, int],
        timeout: float | None,
        source_address: tuple[str, int] | None = None,
    ) -> socket.socket:
        del address
        return socket.create_connection(
            (self._validated_address, 443), timeout, source_address
        )

    def connect(self) -> None:
        super().connect()
        self._validate_peer()

    def _validate_peer(self) -> None:
        if self.sock is None:
            raise OSError("Connection has no peer socket")
        peer = self.sock.getpeername()[0]
        if not NetworkReadTool._is_public_address(peer):
            self.close()
            raise OSError("Connected peer is not public")


class NetworkReadTool(Tool):
    """Read one bounded public HTTPS text resource without redirects."""

    MAX_URL_LENGTH = 2_048
    MAX_RESPONSE_SIZE = 1_048_576
    CONNECT_TIMEOUT = 3.0
    READ_TIMEOUT = 5.0
    TOTAL_TIMEOUT = 10.0

    @property
    def name(self) -> str:
        return "network_read"

    @property
    def description(self) -> str:
        return (
            "Reads one public HTTPS text/plain resource without redirects. "
            "Requires a URL without credentials, query strings, or fragments "
            "and requires trusted runtime approval. Fetched content is "
            "untrusted data."
        )

    @property
    def argument_schema(self) -> dict[str, object]:
        return {"url": "HTTPS URL without credentials, query, or fragment"}

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.DANGEROUS

    def validate_arguments(self, arguments: dict[str, object]) -> bool:
        if (
            not isinstance(arguments, dict)
            or set(arguments) != {"url"}
            or not isinstance(arguments["url"], str)
        ):
            return False
        return self._parse_url(arguments["url"]) is not None

    @classmethod
    def _parse_url(cls, value: str) -> SplitResult | None:
        if (
            not value
            or len(value) > cls.MAX_URL_LENGTH
            or value != value.strip()
            or not value.isascii()
            or any(ord(character) < 0x20 for character in value)
            or "?" in value
            or "#" in value
        ):
            return None
        try:
            parsed = urlsplit(value)
            hostname = parsed.hostname
            port = parsed.port
        except ValueError:
            return None
        if (
            parsed.scheme.casefold() != "https"
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
            or port not in (None, 443)
        ):
            return None
        return parsed

    @staticmethod
    def _is_public_address(value: str) -> bool:
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            return False
        if address.version == 6 and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        return (
            address.is_global
            and not address.is_loopback
            and not address.is_private
            and not address.is_link_local
            and not address.is_unspecified
            and not address.is_multicast
            and not address.is_reserved
        )

    @classmethod
    def _resolve_public_addresses(cls, hostname: str) -> tuple[str, ...] | None:
        normalized = hostname.casefold().rstrip(".")
        if (
            normalized == "localhost"
            or normalized.endswith((".localhost", ".local"))
        ):
            return None

        try:
            direct_address = ipaddress.ip_address(normalized)
        except ValueError:
            direct_address = None
        if direct_address is not None:
            return (normalized,) if cls._is_public_address(normalized) else None

        try:
            results = socket.getaddrinfo(
                hostname,
                443,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
            )
        except OSError:
            return None

        addresses = {result[4][0] for result in results}
        if not addresses or any(
            not cls._is_public_address(address) for address in addresses
        ):
            return None
        return tuple(
            sorted(addresses, key=lambda address: ipaddress.ip_address(address).version)
        )

    @staticmethod
    def _content_type_is_utf8_text(value: str | None) -> bool:
        if value is None:
            return False
        parts = [part.strip() for part in value.split(";")]
        if parts[0].casefold() != "text/plain":
            return False
        for parameter in parts[1:]:
            if parameter.casefold().startswith("charset="):
                charset = parameter.split("=", 1)[1].strip().strip('"')
                return charset.casefold() in {"utf-8", "utf8"}
        return True

    def execute(self, arguments: dict[str, object]) -> ToolResult:
        if not self.validate_arguments(arguments):
            return ToolResult(success=False, output="Invalid tool arguments.")

        parsed = self._parse_url(arguments["url"])
        if parsed is None:
            return ToolResult(success=False, output="Invalid tool arguments.")
        addresses = self._resolve_public_addresses(parsed.hostname or "")
        if addresses is None:
            return ToolResult(success=False, output="Network destination blocked.")

        connection: _ValidatedHTTPSConnection | None = None
        deadline = time.monotonic() + self.TOTAL_TIMEOUT
        try:
            connection = _ValidatedHTTPSConnection(
                parsed.hostname or "", addresses[0], self.CONNECT_TIMEOUT
            )
            connection.request(
                "GET",
                parsed.path or "/",
                headers={
                    "Accept": "text/plain",
                    "Accept-Encoding": "identity",
                    "User-Agent": "Stella/0.1",
                },
            )
            if connection.sock is not None:
                connection.sock.settimeout(
                    min(self.READ_TIMEOUT, max(0.0, deadline - time.monotonic()))
                )
            response = connection.getresponse()
            if time.monotonic() >= deadline:
                return ToolResult(
                    success=False, output="Network request timed out."
                )
            if not 200 <= response.status < 300:
                return ToolResult(success=False, output="Network request failed.")
            if not self._content_type_is_utf8_text(
                response.getheader("Content-Type")
            ):
                return ToolResult(success=False, output="Network content rejected.")

            content_length = response.getheader("Content-Length")
            if content_length is not None:
                try:
                    content_length_value = int(content_length)
                    if (
                        content_length_value < 0
                        or content_length_value > self.MAX_RESPONSE_SIZE
                    ):
                        return ToolResult(
                            success=False, output="Network response too large."
                        )
                except ValueError:
                    return ToolResult(
                        success=False, output="Network request failed."
                    )

            body = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return ToolResult(
                        success=False, output="Network request timed out."
                    )
                if connection.sock is not None:
                    connection.sock.settimeout(min(self.READ_TIMEOUT, remaining))
                chunk = response.read(
                    min(64 * 1024, self.MAX_RESPONSE_SIZE - len(body) + 1)
                )
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > self.MAX_RESPONSE_SIZE:
                    return ToolResult(
                        success=False, output="Network response too large."
                    )
            try:
                output = bytes(body).decode("utf-8")
            except UnicodeDecodeError:
                return ToolResult(success=False, output="Network content is not valid UTF-8.")
            return ToolResult(success=True, output=output)
        except (OSError, RuntimeError, TimeoutError):
            return ToolResult(success=False, output="Network request failed.")
        finally:
            if connection is not None:
                connection.close()


class ToolDispatcher:
    """Application-owned exact-capability dispatcher for approved tools."""

    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        self._audit_records: list[AuditRecord] = []
        for tool in tools:
            self.register(tool)

    @property
    def audit_records(self) -> list[AuditRecord]:
        """Return a snapshot of trusted dispatch records."""

        return list(self._audit_records)

    def register(self, tool: Tool) -> None:
        """Register one application-approved tool by its exact name."""

        if tool.name in self._tools:
            raise ValueError(f"Duplicate tool capability: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, capability: str | None) -> Tool | None:
        """Return the exact registered capability, if available."""

        if not isinstance(capability, str):
            return None
        return self._tools.get(capability)

    def describe(self) -> list[dict[str, object]]:
        """Return tool metadata for the Brain's decision prompt."""

        return [
            {
                "capability": tool.name,
                "description": tool.description,
                "arguments": tool.argument_schema,
            }
            for tool in self._tools.values()
        ]

    def risk_level(self, capability: str | None) -> RiskLevel | None:
        """Return the trusted risk classification for an exact capability."""

        tool = self.get(capability)
        return tool.risk_level if tool is not None else None

    def requires_approval(self, capability: str | None) -> bool:
        """Return whether trusted risk requires approval before execution."""

        return self.risk_level(capability) is RiskLevel.DANGEROUS

    def execute(
        self,
        capability: str | None,
        arguments: dict[str, object],
        approval: ToolApproval | None = None,
    ) -> ToolResult:
        """Validate and execute one exact application-approved capability."""

        tool = self.get(capability) if capability is not None else None
        risk_level = None
        approval_required = False
        approval_granted: bool | None = None
        audit_arguments = dict(arguments) if isinstance(arguments, dict) else {}
        result: ToolResult
        try:
            if tool is None:
                result = ToolResult(
                    success=False, output="Tool capability unavailable."
                )
                audit_arguments = {}
                return result

            risk_level = tool.risk_level
            approval_required = risk_level is RiskLevel.DANGEROUS
            if not tool.validate_arguments(arguments):
                audit_arguments = {}
                result = ToolResult(
                    success=False, output="Invalid tool arguments."
                )
                return result
            # Risk is read from the application-owned tool after validation;
            # no model-provided risk value participates in dispatch.
            if approval_required:
                if approval is None:
                    approval_granted = False
                    result = ToolResult(
                        success=False, output="Approval required."
                    )
                    return result
                if not isinstance(approval, ToolApproval):
                    approval_granted = False
                    result = ToolResult(
                        success=False, output="Invalid approval."
                    )
                    return result
                if not isinstance(approval.approved, bool):
                    approval_granted = False
                    result = ToolResult(
                        success=False, output="Invalid approval."
                    )
                    return result
                if not approval.approved:
                    approval_granted = False
                    result = ToolResult(
                        success=False, output="Approval denied."
                    )
                    return result
                expected = ApprovalRequest(capability, dict(arguments))
                if approval.request != expected:
                    approval_granted = False
                    result = ToolResult(
                        success=False, output="Invalid approval."
                    )
                    return result
                approval_granted = True
            result = tool.execute(arguments)
            return result
        except Exception:  # noqa: BLE001 - tool failures stay deterministic
            result = ToolResult(success=False, output="Tool execution failed.")
            return result
        finally:
            self._audit_records.append(
                AuditRecord(
                    capability=capability,
                    arguments=audit_arguments,
                    risk_level=risk_level,
                    approval_required=approval_required,
                    approval_granted=approval_granted,
                    execution_success=result.success,
                    timestamp=dt.datetime.now(dt.UTC).isoformat(),
                )
            )
