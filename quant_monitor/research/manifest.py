"""Immutable manifests and collision-free hashes for offline research runs.

The module performs no I/O.  Canonicalization v2 uses an escaped type-tag
encoding so Python semantic types cannot silently collapse into ordinary JSON
values.  It also rejects credentials and machine-local paths before a manifest
can be serialized or hashed.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Any, Dict, Iterable, Mapping, MutableSet, Tuple
from urllib.parse import parse_qsl, unquote, unquote_plus, urlsplit

from quant_monitor.domain import ensure_market_time


CANONICALIZATION_VERSION = "quant-monitor-canonical-json-v2"
MANIFEST_SCHEMA_VERSION = "2"

_TYPE_TAG = "$quant_monitor_type"
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_CAMEL_BOUNDARY_RE = re.compile(r"([a-z0-9])([A-Z])")
_FILE_URI_RE = re.compile(r"\bfile\s*:", re.IGNORECASE)
_WINDOWS_DRIVE_PATH_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]")
_WINDOWS_ROOTED_PATH_RE = re.compile(
    r"(?:^|[\s=\"'([{,;])(?:\\\\(?:[?.]\\)?[^\\/\s]+[\\/]|\\(?!\\)[^\s])"
)
_FORWARD_UNC_PATH_RE = re.compile(
    r"(?:^|[\s=\"'([{,;])//[^/\s]+/[^\s]+"
)
_POSIX_PATH_RE = re.compile(r"(?:^|[\s=\"'([{,;:])/(?!/)(?:[^\s]|$)")
_HOME_PATH_RE = re.compile(r"(?:^|[\s=\"'([{,;:])~[\\/]")
_URL_RE = re.compile(r"(?:https?|wss?|ftp)://[^\s<>\"']+", re.IGNORECASE)
_PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", re.IGNORECASE)
_LIVE_CREDENTIAL_RE = re.compile(
    r"(?:"
    r"\bash_[A-Za-z0-9]{32,}\b|"
    r"\b[0-9a-f]{32}-c-app\b|"
    r"\bsk-[A-Za-z0-9_-]{16,}\b|"
    r"\bAKIA[0-9A-Z]{16}\b|"
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
    r")",
    re.IGNORECASE,
)
_AUTH_VALUE_RE = re.compile(r"^(?:bearer|basic)\s+[^\s,;]+\s*$", re.IGNORECASE)
_AUTH_TOKEN_RE = re.compile(
    r"\b(?:bearer|basic)\s+(?=[^\s,;]{8,})(?=[^\s,;]*(?:\d|[._=-]))[^\s,;]+",
    re.IGNORECASE,
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?:"
    r"\b(?:api\s*[_-]?\s*key|auth\s*[_-]?\s*key|access\s*[_-]?\s*token|"
    r"refresh\s*[_-]?\s*token|client\s*[_-]?\s*secret|private\s*[_-]?\s*key|"
    r"authorization|password|passwd|secret|cookie|token)\b|"
    r"(?:API\s*)?(?:密钥|秘钥)|密码|口令|访问令牌|刷新令牌|授权|凭据|凭证"
    r")\s*(?::|=|：)\s*\S+",
    re.IGNORECASE,
)
_API_KEY_PROSE_RE = re.compile(
    r"\bapi\s*[_-]?\s*key\s+(?:is\s+)?[A-Za-z0-9][A-Za-z0-9_.-]{7,}\b",
    re.IGNORECASE,
)
_SENSITIVE_WORDS = frozenset(
    {
        "auth",
        "authorization",
        "cookie",
        "credential",
        "credentials",
        "passwd",
        "password",
        "secret",
        "signature",
        "token",
    }
)
_SENSITIVE_COMPACT_PARTS = (
    "apikey",
    "appkey",
    "appsecret",
    "authkey",
    "authtoken",
    "accesskey",
    "accesstoken",
    "refreshtoken",
    "bearertoken",
    "clientsecret",
    "consumerkey",
    "consumersecret",
    "privatekey",
    "proxyauthorization",
    "sessionkey",
    "setcookie",
    "subscriptionkey",
    "xapikey",
)
_SENSITIVE_CHINESE_PARTS = (
    "密钥",
    "秘钥",
    "密码",
    "口令",
    "令牌",
    "授权",
    "凭据",
    "凭证",
)
_SAFE_TOKEN_FIELD_NAMES = frozenset(
    {
        "tokencount",
        "tokencounts",
        "tokenization",
        "tokenizer",
        "tokenizers",
    }
)
_SAFE_TOKEN_SUFFIXES = frozenset({"count", "counts"})


class CanonicalizationError(ValueError):
    """Raised when a value has no safe deterministic representation."""


class ManifestValidationError(ValueError):
    """Raised when a research manifest violates its public contract."""


class SensitiveManifestValueError(ManifestValidationError):
    """Raised without echoing a credential or machine-local path value."""


class _FrozenList(tuple):
    """Tuple-backed immutable copy which retains original list semantics."""

    def __eq__(self, other: object) -> bool:
        return type(other) is _FrozenList and tuple.__eq__(self, other)

    __hash__ = tuple.__hash__


class _FrozenSet(tuple):
    """Tuple-backed immutable copy which retains original set semantics."""

    def __eq__(self, other: object) -> bool:
        return type(other) is _FrozenSet and tuple.__eq__(self, other)

    __hash__ = tuple.__hash__


class _FrozenFrozenset(tuple):
    """Tuple-backed immutable copy which retains frozenset semantics."""

    def __eq__(self, other: object) -> bool:
        return type(other) is _FrozenFrozenset and tuple.__eq__(self, other)

    __hash__ = tuple.__hash__


@dataclass(frozen=True)
class _FrozenEnumValue:
    type_name: str
    member_name: str
    value: Any


@dataclass(frozen=True)
class _FrozenDataclassValue:
    type_name: str
    values: Tuple[Tuple[str, Any], ...]


@dataclass(frozen=True)
class _FrozenDatetimeValue:
    """Canonical instant detached from a caller-controlled ``tzinfo``."""

    value: str


@dataclass(frozen=True)
class _FrozenTimeValue:
    """Canonical wall time retained separately from ordinary strings."""

    value: str
    fold: int


def _field_words(key: str) -> Tuple[str, ...]:
    separated = _CAMEL_BOUNDARY_RE.sub(r"\1_\2", key)
    return tuple(item for item in re.split(r"[^A-Za-z0-9]+", separated.lower()) if item)


def _security_scan_text(value: str) -> str:
    """Normalize compatibility forms and remove invisible format separators.

    This representation is used only by the deny-list scanners.  Canonical
    output continues to preserve safe caller text byte-for-byte.
    """

    normalized = unicodedata.normalize("NFKC", value)
    return "".join(
        character
        for character in normalized
        if unicodedata.category(character) != "Cf"
    )


def _decoded_variants(value: str) -> Tuple[str, ...]:
    """Return bounded URL-decoding variants, including double encoding."""

    variants = []
    pending = [value]
    seen = set()
    for _ in range(3):
        next_pending = []
        for item in pending:
            if item in seen:
                continue
            seen.add(item)
            variants.append(item)
            try:
                decoded_items = (unquote(item), unquote_plus(item))
            except Exception:
                raise CanonicalizationError("unable to decode text safely") from None
            for decoded in decoded_items:
                if decoded not in seen:
                    next_pending.append(decoded)
        pending = next_pending
        if not pending:
            break
    return tuple(variants)


def _validate_codepoints(value: str, path: str) -> None:
    if "\x00" in value:
        raise CanonicalizationError("NUL characters are not permitted at {}".format(path))
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise CanonicalizationError(
            "surrogate code points are not permitted at {}".format(path)
        )


def _is_sensitive_field_name(value: str) -> bool:
    for candidate in _decoded_variants(value):
        candidate = _security_scan_text(candidate)
        compact_chinese = re.sub(r"\s+", "", candidate)
        # “令牌化” is an ordinary ML term.  Remove that complete term before
        # scanning, while still rejecting combinations such as “密码令牌化”.
        chinese_without_tokenization = compact_chinese.replace("令牌化", "")
        if any(
            part in chinese_without_tokenization for part in _SENSITIVE_CHINESE_PARTS
        ):
            return True
        words = _field_words(candidate)
        compact = "".join(words)
        if compact in _SAFE_TOKEN_FIELD_NAMES:
            continue
        if any(part in compact for part in _SENSITIVE_COMPACT_PARTS):
            return True
        sensitive_words = []
        for index, word in enumerate(words):
            if word == "token" and index + 1 < len(words):
                if words[index + 1] in _SAFE_TOKEN_SUFFIXES:
                    continue
            if word in _SENSITIVE_WORDS:
                sensitive_words.append(word)
        if sensitive_words:
            return True
    return False


def _contains_absolute_path(value: str) -> bool:
    value = _security_scan_text(value)
    return bool(
        _FILE_URI_RE.search(value)
        or _WINDOWS_DRIVE_PATH_RE.search(value)
        or _WINDOWS_ROOTED_PATH_RE.search(value)
        or _FORWARD_UNC_PATH_RE.search(value)
        or _POSIX_PATH_RE.search(value)
        or _HOME_PATH_RE.search(value)
    )


def _url_contains_secret(value: str) -> bool:
    value = _security_scan_text(value)
    candidates = list(_URL_RE.findall(value))
    stripped = value.strip()
    if stripped.startswith("//"):
        candidates.append("https:" + stripped)
    for candidate in candidates:
        try:
            parsed = urlsplit(candidate)
            if "@" in parsed.netloc:
                return True
            for key, _ in parse_qsl(parsed.query, keep_blank_values=True):
                if _is_sensitive_field_name(key):
                    return True
        except (TypeError, ValueError, UnicodeError):
            # Malformed URL-looking text is not itself proof of a secret.  The
            # remaining scanners still examine assignments and key shapes.
            continue
    return False


def _plain_text_is_sensitive(value: str) -> bool:
    value = _security_scan_text(value)
    stripped = value.strip()
    return bool(
        _PRIVATE_KEY_RE.search(value)
        or _LIVE_CREDENTIAL_RE.search(value)
        or _SECRET_ASSIGNMENT_RE.search(value)
        or _API_KEY_PROSE_RE.search(value)
        or _AUTH_VALUE_RE.fullmatch(stripped)
        or _AUTH_TOKEN_RE.search(value)
        or _url_contains_secret(value)
    )


def _validate_embedded_json(value: str, path: str) -> None:
    stripped = value.strip()
    if not stripped or stripped[0] not in "[{":
        return
    try:
        parsed = json.loads(stripped)
    except (TypeError, ValueError, json.JSONDecodeError):
        return

    def scan(item: Any, item_path: str) -> None:
        if isinstance(item, Mapping):
            try:
                snapshot = list(item.items())
            except Exception:
                raise CanonicalizationError(
                    "unable to inspect embedded JSON at {}".format(item_path)
                ) from None
            for index, pair in enumerate(snapshot):
                try:
                    key, nested = pair
                except (TypeError, ValueError):
                    raise CanonicalizationError(
                        "invalid embedded JSON object at {}".format(item_path)
                    ) from None
                if isinstance(key, str) and _is_sensitive_field_name(key):
                    raise SensitiveManifestValueError(
                        "credential-bearing JSON fields are not permitted at {}".format(
                            item_path
                        )
                    )
                scan(nested, "{}.<item#{}>".format(item_path, index))
        elif isinstance(item, list):
            for index, nested in enumerate(item):
                scan(nested, "{}[{}]".format(item_path, index))
        elif isinstance(item, str):
            _validate_text(item, item_path, inspect_json=False)

    scan(parsed, path)


def _validate_text(value: str, path: str, *, inspect_json: bool = True) -> str:
    for candidate in _decoded_variants(value):
        _validate_codepoints(candidate, path)
        if _contains_absolute_path(candidate):
            raise SensitiveManifestValueError(
                "absolute machine-local paths are not permitted at {}".format(path)
            )
        if _plain_text_is_sensitive(candidate):
            raise SensitiveManifestValueError(
                "credential-like values are not permitted at {}".format(path)
            )
        if inspect_json:
            _validate_embedded_json(candidate, path)
    return value


def _validate_key(key: Any, path: str) -> str:
    if type(key) is not str:
        raise CanonicalizationError("mapping keys must be strings at {}".format(path))
    if not key:
        raise CanonicalizationError("mapping keys cannot be empty at {}".format(path))
    _validate_text(key, path, inspect_json=False)
    if _is_sensitive_field_name(key):
        raise SensitiveManifestValueError(
            "credential-bearing field names are not permitted at {}".format(path)
        )
    return key


def _decimal_text(value: Decimal, path: str) -> str:
    if not value.is_finite():
        raise CanonicalizationError("Decimal values must be finite at {}".format(path))
    if value == 0:
        return "0e0"
    parts = value.as_tuple()
    digits = list(parts.digits)
    exponent = int(parts.exponent)
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1
    coefficient = "".join(str(item) for item in digits)
    sign = "-" if parts.sign else ""
    return "{}{}e{}".format(sign, coefficient, exponent)


def _type_name(value_type: type, path: str) -> str:
    try:
        value = "{}.{}".format(value_type.__module__, value_type.__qualname__)
    except Exception:
        raise CanonicalizationError("unable to identify typed value at {}".format(path)) from None
    return _validate_text(value, path, inspect_json=False)


def _snapshot_mapping(value: Mapping[Any, Any], path: str) -> Tuple[Tuple[str, Any], ...]:
    try:
        raw_items = list(value.items())
    except Exception:
        raise CanonicalizationError("unable to snapshot mapping at {}".format(path)) from None
    result = []
    seen = set()
    for index, pair in enumerate(raw_items):
        try:
            raw_key, raw_value = pair
        except (TypeError, ValueError):
            raise CanonicalizationError("invalid mapping item at {}".format(path)) from None
        key = _validate_key(raw_key, "{}.<key#{}>".format(path, index))
        if key in seen:
            raise CanonicalizationError("duplicate mapping keys are not permitted at {}".format(path))
        seen.add(key)
        result.append((key, raw_value))
    result.sort(key=lambda item: item[0])
    return tuple(result)


def _tag(kind: str, **values: Any) -> Dict[str, Any]:
    result = {_TYPE_TAG: kind}
    result.update(values)
    return result


def _canonical_datetime(value: datetime, path: str) -> str:
    try:
        offset = value.utcoffset() if value.tzinfo is not None else None
    except Exception:
        raise CanonicalizationError(
            "datetime timezone information is invalid at {}".format(path)
        ) from None
    if offset is None:
        raise CanonicalizationError(
            "datetime values must be timezone-aware at {}".format(path)
        )
    try:
        # Snapshot the offset once.  Calling ``astimezone`` on the original
        # object would re-enter a mutable/custom tzinfo after validation.
        fixed = value.replace(tzinfo=timezone(offset))
        return ensure_market_time(fixed).isoformat(timespec="microseconds")
    except Exception:
        raise CanonicalizationError(
            "datetime values could not be normalized at {}".format(path)
        ) from None


def _canonicalize(value: Any, path: str, active: MutableSet[int]) -> Any:
    if isinstance(value, _FrozenDatetimeValue):
        return _tag("datetime", value=value.value)
    if isinstance(value, _FrozenTimeValue):
        return _tag("time", value=value.value, fold=value.fold)
    if isinstance(value, _FrozenEnumValue):
        return _tag(
            "enum",
            type=value.type_name,
            name=value.member_name,
            value=_canonicalize(value.value, "{}.value".format(path), active),
        )
    if isinstance(value, _FrozenDataclassValue):
        return _tag(
            "dataclass",
            type=value.type_name,
            fields=[
                [name, _canonicalize(item, "{}.<field#{}>".format(path, index), active)]
                for index, (name, item) in enumerate(value.values)
            ],
        )
    if isinstance(value, Enum):
        return _tag(
            "enum",
            type=_type_name(type(value), path),
            name=_validate_text(value.name, "{}.name".format(path), inspect_json=False),
            value=_canonicalize(value.value, "{}.value".format(path), active),
        )
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is str:
        return _validate_text(value, path)
    if type(value) is Decimal:
        return _tag("decimal", value=_decimal_text(value, path))
    if type(value) is float:
        if not math.isfinite(value):
            raise CanonicalizationError("float values must be finite at {}".format(path))
        return 0.0 if value == 0.0 else value
    if type(value) is datetime:
        return _tag("datetime", value=_canonical_datetime(value, path))
    if type(value) is date:
        return _tag("date", value=value.isoformat())
    if type(value) is time:
        if value.tzinfo is not None:
            raise CanonicalizationError(
                "time values must not carry tzinfo at {}".format(path)
            )
        return _tag(
            "time",
            value=value.isoformat(timespec="microseconds"),
            fold=value.fold,
        )

    track_cycle = is_dataclass(value) or isinstance(
        value, (Mapping, list, tuple, set, frozenset)
    )
    identity = id(value)
    if track_cycle:
        if identity in active:
            raise CanonicalizationError("cyclic values are not permitted at {}".format(path))
        active.add(identity)
    try:
        if is_dataclass(value) and not isinstance(value, type):
            encoded_fields = []
            for index, item in enumerate(fields(value)):
                name = _validate_key(item.name, "{}.<field-name#{}>".format(path, index))
                try:
                    raw = getattr(value, item.name)
                except Exception:
                    raise CanonicalizationError(
                        "unable to read dataclass field at {}".format(path)
                    ) from None
                encoded_fields.append(
                    [name, _canonicalize(raw, "{}.<field#{}>".format(path, index), active)]
                )
            return _tag("dataclass", type=_type_name(type(value), path), fields=encoded_fields)
        if isinstance(value, Mapping):
            snapshot = _snapshot_mapping(value, path)
            encoded = {}
            for index, (key, raw) in enumerate(snapshot):
                try:
                    encoded[key] = _canonicalize(
                        raw, "{}.<value#{}>".format(path, index), active
                    )
                except (CanonicalizationError, ManifestValidationError):
                    raise
                except Exception:
                    raise CanonicalizationError(
                        "unable to canonicalize mapping value at {}".format(path)
                    ) from None
            if _TYPE_TAG in encoded:
                return _tag(
                    "mapping",
                    items=[[key, encoded[key]] for key in sorted(encoded)],
                )
            return encoded
        if isinstance(value, _FrozenList):
            return [
                _canonicalize(item, "{}[{}]".format(path, index), active)
                for index, item in enumerate(value)
            ]
        if type(value) is tuple:
            return _tag(
                "tuple",
                items=[
                    _canonicalize(item, "{}[{}]".format(path, index), active)
                    for index, item in enumerate(value)
                ],
            )
        if type(value) is list:
            return [
                _canonicalize(item, "{}[{}]".format(path, index), active)
                for index, item in enumerate(value)
            ]
        if isinstance(value, _FrozenSet):
            items = [
                _canonicalize(item, "{}[set]".format(path), active) for item in value
            ]
            return _tag("set", items=sorted(items, key=_dump_canonical))
        if isinstance(value, _FrozenFrozenset):
            items = [
                _canonicalize(item, "{}[frozenset]".format(path), active)
                for item in value
            ]
            return _tag("frozenset", items=sorted(items, key=_dump_canonical))
        if type(value) is frozenset:
            items = [
                _canonicalize(item, "{}[frozenset]".format(path), active)
                for item in value
            ]
            return _tag("frozenset", items=sorted(items, key=_dump_canonical))
        if type(value) is set:
            items = [
                _canonicalize(item, "{}[set]".format(path), active) for item in value
            ]
            return _tag("set", items=sorted(items, key=_dump_canonical))
    finally:
        if track_cycle:
            active.remove(identity)

    raise CanonicalizationError("unsupported value type at {}".format(path))


def _dump_canonical(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, UnicodeError):
        raise CanonicalizationError("unable to encode canonical JSON") from None


def _normalized(value: Any, path: str = "$") -> Any:
    return _canonicalize(value, path, set())


def canonical_json(value: Any) -> str:
    """Return collision-free deterministic JSON for supported safe values."""

    try:
        return _dump_canonical(_normalized(value))
    except (CanonicalizationError, ManifestValidationError):
        raise
    except Exception:
        raise CanonicalizationError("unable to canonicalize value") from None


def _digest_json_text(value: str) -> str:
    try:
        payload = value.encode("utf-8")
    except UnicodeError:
        raise CanonicalizationError("canonical JSON is not valid UTF-8") from None
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _identifier(value: Any, path: str) -> str:
    if type(value) is not str:
        raise ManifestValidationError("{} must be a string".format(path))
    if not value or not value.strip():
        raise ManifestValidationError("{} cannot be empty".format(path))
    if value != value.strip():
        raise ManifestValidationError("{} cannot have surrounding whitespace".format(path))
    if len(value) > 500:
        raise ManifestValidationError("{} is too long".format(path))
    return _validate_text(value, path)


def hash_canonical(value: Any, *, namespace: str = "research-value-v2") -> str:
    """Hash one canonical value with a strict explicit domain namespace."""

    namespace_value = _identifier(namespace, "$.namespace")
    return _digest_json_text(
        canonical_json(
            {
                "canonicalization": CANONICALIZATION_VERSION,
                "namespace": namespace_value,
                "value": value,
            }
        )
    )


def hash_input_records(records: Iterable[Any]) -> str:
    """Hash an order-independent but duplicate-sensitive record multiset."""

    if isinstance(records, (str, bytes, bytearray, Mapping)):
        raise CanonicalizationError("records must be an iterable of records")
    normalized_records = []
    try:
        for index, record in enumerate(records):
            normalized_records.append(_normalized(record, "$[{}]".format(index)))
    except (CanonicalizationError, ManifestValidationError):
        raise
    except Exception:
        raise CanonicalizationError("unable to enumerate input records") from None
    normalized_records.sort(key=_dump_canonical)
    # These values are already canonical trees.  Wrapping them as an ordinary
    # list is unambiguous and avoids interpreting vendor return order.
    envelope = {
        "canonicalization": CANONICALIZATION_VERSION,
        "namespace": "research-input-record-multiset-v2",
        "records": normalized_records,
    }
    return _digest_json_text(_dump_canonical(envelope))


def _freeze_semantic(value: Any, path: str, active: MutableSet[int]) -> Any:
    if isinstance(value, Enum):
        return _FrozenEnumValue(
            type_name=_type_name(type(value), path),
            member_name=_validate_text(value.name, "{}.name".format(path), inspect_json=False),
            value=_freeze_semantic(value.value, "{}.value".format(path), active),
        )
    if value is None or type(value) in (bool, int, date, Decimal):
        _canonicalize(value, path, set())
        return value
    if type(value) is datetime:
        return _FrozenDatetimeValue(_canonical_datetime(value, path))
    if type(value) is time:
        _canonicalize(value, path, set())
        return _FrozenTimeValue(
            value=value.isoformat(timespec="microseconds"),
            fold=value.fold,
        )
    if type(value) is str:
        return _validate_text(value, path)
    if type(value) is float:
        _canonicalize(value, path, set())
        return 0.0 if value == 0.0 else value

    # Reject unsupported subclasses consistently before container dispatch.
    # Their overridden iteration/comparison methods could otherwise make a
    # supposedly immutable manifest depend on mutable process behavior.
    if isinstance(value, (str, bool, int, float, Decimal, date, time)):
        raise CanonicalizationError("unsupported value type at {}".format(path))

    track_cycle = is_dataclass(value) or isinstance(
        value, (Mapping, list, tuple, set, frozenset)
    )
    identity = id(value)
    if track_cycle:
        if identity in active:
            raise CanonicalizationError("cyclic values are not permitted at {}".format(path))
        active.add(identity)
    try:
        if is_dataclass(value) and not isinstance(value, type):
            frozen_fields = []
            for index, item in enumerate(fields(value)):
                name = _validate_key(item.name, "{}.<field-name#{}>".format(path, index))
                try:
                    raw = getattr(value, item.name)
                except Exception:
                    raise CanonicalizationError(
                        "unable to read dataclass field at {}".format(path)
                    ) from None
                frozen_fields.append(
                    (
                        name,
                        _freeze_semantic(raw, "{}.<field#{}>".format(path, index), active),
                    )
                )
            return _FrozenDataclassValue(_type_name(type(value), path), tuple(frozen_fields))
        if isinstance(value, Mapping):
            snapshot = _snapshot_mapping(value, path)
            return MappingProxyType(
                {
                    key: _freeze_semantic(
                        raw, "{}.<value#{}>".format(path, index), active
                    )
                    for index, (key, raw) in enumerate(snapshot)
                }
            )
        if type(value) is list:
            return _FrozenList(
                _freeze_semantic(item, "{}[{}]".format(path, index), active)
                for index, item in enumerate(value)
            )
        if type(value) is tuple:
            return tuple(
                _freeze_semantic(item, "{}[{}]".format(path, index), active)
                for index, item in enumerate(value)
            )
        if type(value) is set:
            frozen_items = [
                _freeze_semantic(item, "{}[set]".format(path), active) for item in value
            ]
            frozen_items.sort(key=lambda item: _dump_canonical(_canonicalize(item, path, set())))
            return _FrozenSet(frozen_items)
        if type(value) is frozenset:
            frozen_items = [
                _freeze_semantic(item, "{}[frozenset]".format(path), active)
                for item in value
            ]
            frozen_items.sort(key=lambda item: _dump_canonical(_canonicalize(item, path, set())))
            return _FrozenFrozenset(frozen_items)
    finally:
        if track_cycle:
            active.remove(identity)
    raise CanonicalizationError("unsupported value type at {}".format(path))


def _frozen_mapping(value: Mapping[str, Any], path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ManifestValidationError("{} must be a mapping".format(path))
    frozen = _freeze_semantic(value, path, set())
    if not isinstance(frozen, Mapping):
        raise ManifestValidationError("{} must be a mapping".format(path))
    return frozen


@dataclass(frozen=True)
class DatasetDescriptor:
    """Description of the normalized dataset used by a research run."""

    name: str
    schema_version: str
    source: str
    record_count: int
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _identifier(self.name, "$.dataset.name"))
        object.__setattr__(
            self,
            "schema_version",
            _identifier(self.schema_version, "$.dataset.schema_version"),
        )
        object.__setattr__(self, "source", _identifier(self.source, "$.dataset.source"))
        if type(self.record_count) is not int:
            raise ManifestValidationError("dataset record_count must be an integer")
        if self.record_count < 0:
            raise ManifestValidationError("dataset record_count cannot be negative")
        object.__setattr__(
            self,
            "attributes",
            _frozen_mapping(self.attributes, "$.dataset.attributes"),
        )

    def _content_value(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "schema_version": self.schema_version,
            "source": self.source,
            "record_count": self.record_count,
            "attributes": self.attributes,
        }

    def to_dict(self) -> Dict[str, Any]:
        return _normalized(self._content_value(), "$.dataset")


@dataclass(frozen=True)
class ResearchRunManifest:
    """Immutable identity and content manifest for one offline research run."""

    run_id: str
    strategy_name: str
    strategy_parameters: Mapping[str, Any]
    strategy_code_version: str
    dataset: DatasetDescriptor
    input_hash: str
    calendar_version: str
    universe_version: str
    adjustment_version: str
    constraint_version: str
    configuration: Mapping[str, Any]
    assumptions: Mapping[str, Any]
    manifest_schema_version: str = MANIFEST_SCHEMA_VERSION
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
            "run_id",
            "strategy_name",
            "strategy_code_version",
            "calendar_version",
            "universe_version",
            "adjustment_version",
            "constraint_version",
            "manifest_schema_version",
        ):
            object.__setattr__(
                self,
                name,
                _identifier(getattr(self, name), "$.{}".format(name)),
            )
        if self.manifest_schema_version != MANIFEST_SCHEMA_VERSION:
            raise ManifestValidationError(
                "manifest_schema_version must match the current schema version"
            )
        if not isinstance(self.dataset, DatasetDescriptor):
            raise ManifestValidationError("dataset must be a DatasetDescriptor")
        if type(self.input_hash) is not str:
            raise ManifestValidationError("input_hash must be a string")
        if self.input_hash != self.input_hash.strip() or not _HASH_RE.fullmatch(
            self.input_hash
        ):
            raise ManifestValidationError(
                "input_hash must use the sha256:<64 lowercase hex> form"
            )
        for name in ("strategy_parameters", "configuration", "assumptions"):
            object.__setattr__(
                self,
                name,
                _frozen_mapping(getattr(self, name), "$.{}".format(name)),
            )
        object.__setattr__(
            self,
            "content_hash",
            hash_canonical(
                self._content_value(),
                namespace="research-run-manifest-content-v2",
            ),
        )

    def _content_value(self) -> Dict[str, Any]:
        return {
            "manifest_schema_version": self.manifest_schema_version,
            "purpose": "research_only",
            "strategy": {
                "name": self.strategy_name,
                "parameters": self.strategy_parameters,
                "code_version": self.strategy_code_version,
            },
            "dataset": self.dataset._content_value(),
            "input_hash": self.input_hash,
            "versions": {
                "calendar": self.calendar_version,
                "universe": self.universe_version,
                "adjustment": self.adjustment_version,
                "constraints": self.constraint_version,
            },
            "configuration": self.configuration,
            "assumptions": self.assumptions,
        }

    def content_dict(self) -> Dict[str, Any]:
        """Return detached JSON-native hash payload, excluding run identity."""

        return _normalized(self._content_value())

    def to_dict(self) -> Dict[str, Any]:
        """Return a detached JSON-native manifest including identity and hash."""

        value = self._content_value()
        value["run_id"] = self.run_id
        value["content_hash"] = self.content_hash
        return _normalized(value)

    def to_json(self) -> str:
        """Return the complete manifest as canonical JSON."""

        return _dump_canonical(self.to_dict())
