"""校准协议和测点记录的严格数据契约。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence


class ValidationError(ValueError):
    """输入不能满足领域契约。"""

    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


def _require_mapping(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{path} 必须是对象", field=path)
    return value


def _require_sequence(value: object, path: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValidationError(f"{path} 必须是数组", field=path)
    return value


def _required_text(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{path} 必须是非空字符串", field=path)
    return value.strip()


def _optional_text(value: object, path: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, path)


def _decimal(value: object, path: str) -> Decimal:
    if isinstance(value, bool):
        raise ValidationError(f"{path} 必须是数值", field=path)
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError(f"{path} 必须是十进制数值", field=path) from exc
    if not result.is_finite():
        raise ValidationError(f"{path} 必须是有限数值", field=path)
    return result


def _timestamp(value: object, path: str) -> str:
    """observed_at 必须是带时区偏移的 ISO-8601 日期时间。"""

    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{path} 必须是带时区的 ISO-8601 日期时间字符串", field=path)
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValidationError(f"{path} 不是合法的 ISO-8601 日期时间", field=path) from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{path} 必须携带时区偏移（例如 +08:00 或 Z）", field=path)
    return text


@dataclass(frozen=True, slots=True)
class Stratum:
    """一个需要单独覆盖的校准环境分层。"""

    key: str
    label: str
    required_trials: int

    @classmethod
    def from_dict(cls, raw: object, path: str) -> "Stratum":
        data = _require_mapping(raw, path)
        required_trials = data.get("required_trials")
        if isinstance(required_trials, bool) or not isinstance(required_trials, int):
            raise ValidationError(f"{path}.required_trials 必须是整数", field=f"{path}.required_trials")
        if required_trials <= 0:
            raise ValidationError(f"{path}.required_trials 必须大于零", field=f"{path}.required_trials")
        return cls(
            key=_required_text(data.get("key"), f"{path}.key"),
            label=_required_text(data.get("label"), f"{path}.label"),
            required_trials=required_trials,
        )


@dataclass(frozen=True, slots=True)
class Metric:
    """协议中声明的一个可测点指标。"""

    key: str
    label: str
    kind: str
    unit: str | None
    direction: str

    @classmethod
    def from_dict(cls, raw: object, path: str) -> "Metric":
        data = _require_mapping(raw, path)
        kind = _required_text(data.get("kind"), f"{path}.kind")
        if kind not in {"binary", "continuous", "count"}:
            raise ValidationError(f"{path}.kind 不受支持", field=f"{path}.kind")
        direction = _required_text(data.get("direction"), f"{path}.direction")
        if direction not in {"higher", "lower"}:
            raise ValidationError(f"{path}.direction 必须是 higher 或 lower", field=f"{path}.direction")
        unit = _optional_text(data.get("unit"), f"{path}.unit")
        if kind == "binary" and unit is not None:
            raise ValidationError(f"{path}.unit 对二元指标必须为空", field=f"{path}.unit")
        return cls(
            key=_required_text(data.get("key"), f"{path}.key"),
            label=_required_text(data.get("label"), f"{path}.label"),
            kind=kind,
            unit=unit,
            direction=direction,
        )


@dataclass(frozen=True, slots=True)
class Protocol:
    """一次校准所依据的不可歧义协议版本。"""

    protocol_id: str
    version: int
    title: str
    task_family: str
    strata: tuple[Stratum, ...]
    metrics: tuple[Metric, ...]
    stratum_weights: Mapping[str, Decimal]
    seed: int
    bootstrap_samples: int
    admission_rules: tuple[Mapping[str, Any], ...]

    @classmethod
    def from_dict(cls, raw: object) -> "Protocol":
        data = _require_mapping(raw, "protocol")
        version = data.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
            raise ValidationError("protocol.version 必须是正整数", field="protocol.version")
        strata = tuple(
            Stratum.from_dict(item, f"protocol.strata[{index}]")
            for index, item in enumerate(_require_sequence(data.get("strata"), "protocol.strata"))
        )
        metrics = tuple(
            Metric.from_dict(item, f"protocol.metrics[{index}]")
            for index, item in enumerate(_require_sequence(data.get("metrics"), "protocol.metrics"))
        )
        if not strata:
            raise ValidationError("protocol.strata 不能为空", field="protocol.strata")
        if not metrics:
            raise ValidationError("protocol.metrics 不能为空", field="protocol.metrics")
        if len({item.key for item in strata}) != len(strata):
            raise ValidationError("protocol.strata.key 不能重复", field="protocol.strata")
        if len({item.key for item in metrics}) != len(metrics):
            raise ValidationError("protocol.metrics.key 不能重复", field="protocol.metrics")
        raw_weights = _require_mapping(data.get("stratum_weights"), "protocol.stratum_weights")
        if set(raw_weights) != {item.key for item in strata}:
            raise ValidationError(
                "protocol.stratum_weights 必须覆盖全部且仅覆盖已声明分层",
                field="protocol.stratum_weights",
            )
        weights = {
            key: _decimal(value, f"protocol.stratum_weights.{key}")
            for key, value in raw_weights.items()
        }
        if any(value <= 0 for value in weights.values()):
            raise ValidationError(
                "protocol.stratum_weights 必须全部大于零", field="protocol.stratum_weights"
            )
        if sum(weights.values(), Decimal(0)) != Decimal(1):
            raise ValidationError(
                "protocol.stratum_weights 之和必须为 1", field="protocol.stratum_weights"
            )
        seed = data.get("seed")
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise ValidationError("protocol.seed 必须是整数", field="protocol.seed")
        bootstrap_samples = data.get("bootstrap_samples")
        if (
            isinstance(bootstrap_samples, bool)
            or not isinstance(bootstrap_samples, int)
            or bootstrap_samples < 100
            or bootstrap_samples > 100000
        ):
            raise ValidationError(
                "protocol.bootstrap_samples 必须在 100 到 100000 之间",
                field="protocol.bootstrap_samples",
            )
        rules = tuple(
            _require_mapping(item, f"protocol.admission_rules[{index}]")
            for index, item in enumerate(
                _require_sequence(data.get("admission_rules"), "protocol.admission_rules")
            )
        )
        if not rules:
            raise ValidationError(
                "protocol.admission_rules 不能为空", field="protocol.admission_rules"
            )
        for index, rule in enumerate(rules):
            metric = _required_text(rule.get("metric"), f"protocol.admission_rules[{index}].metric")
            if metric not in {item.key for item in metrics}:
                raise ValidationError(
                    f"protocol.admission_rules[{index}].metric 未声明",
                    field=f"protocol.admission_rules[{index}].metric",
                )
            operator = _required_text(
                rule.get("operator"), f"protocol.admission_rules[{index}].operator"
            )
            if operator not in {"gte", "lte"}:
                raise ValidationError(
                    f"protocol.admission_rules[{index}].operator 不受支持",
                    field=f"protocol.admission_rules[{index}].operator",
                )
            _decimal(rule.get("threshold"), f"protocol.admission_rules[{index}].threshold")
        return cls(
            protocol_id=_required_text(data.get("protocol_id"), "protocol.protocol_id"),
            version=version,
            title=_required_text(data.get("title"), "protocol.title"),
            task_family=_required_text(data.get("task_family"), "protocol.task_family"),
            strata=strata,
            metrics=metrics,
            stratum_weights=weights,
            seed=seed,
            bootstrap_samples=bootstrap_samples,
            admission_rules=rules,
        )

    @property
    def metric_map(self) -> dict[str, Metric]:
        return {metric.key: metric for metric in self.metrics}

    @property
    def stratum_keys(self) -> frozenset[str]:
        return frozenset(item.key for item in self.strata)


@dataclass(frozen=True, slots=True)
class Observation:
    """一次已结构化的传感器任务测点。"""

    source_batch: str
    source_row: str
    robot_id: str
    protocol_id: str
    protocol_version: int
    stratum_key: str
    observed_at: str
    metrics: Mapping[str, Decimal]
    excluded_reason: str | None

    @classmethod
    def from_dict(cls, raw: object, protocol: Protocol, *, index: int | None = None) -> "Observation":
        base = "observation" if index is None else f"observations[{index}]"
        data = _require_mapping(raw, base)
        protocol_id = _required_text(data.get("protocol_id"), f"{base}.protocol_id")
        protocol_version = data.get("protocol_version")
        if isinstance(protocol_version, bool) or not isinstance(protocol_version, int):
            raise ValidationError(
                f"{base}.protocol_version 必须是正整数", field=f"{base}.protocol_version"
            )
        if protocol_id != protocol.protocol_id or protocol_version != protocol.version:
            mismatch_field = (
                f"{base}.protocol_id"
                if protocol_id != protocol.protocol_id
                else f"{base}.protocol_version"
            )
            raise ValidationError(
                f"{mismatch_field} 测点引用的协议版本与当前协议不一致", field=mismatch_field
            )
        stratum_key = _required_text(data.get("stratum_key"), f"{base}.stratum_key")
        if stratum_key not in protocol.stratum_keys:
            raise ValidationError(f"{base}.stratum_key 未在协议中声明", field=f"{base}.stratum_key")
        metric_data = _require_mapping(data.get("metrics"), f"{base}.metrics")
        expected = protocol.metric_map
        missing = sorted(set(expected) - set(metric_data))
        extra = sorted(set(metric_data) - set(expected))
        if missing or extra:
            raise ValidationError(
                f"{base}.metrics 测点指标不匹配：缺少 {missing}，多出 {extra}",
                field=f"{base}.metrics",
            )
        parsed: dict[str, Decimal] = {}
        for key, value in metric_data.items():
            metric = expected[key]
            metric_path = f"{base}.metrics.{key}"
            number = _decimal(value, metric_path)
            if metric.kind == "binary" and number not in {Decimal(0), Decimal(1)}:
                raise ValidationError(f"{metric_path} 必须是 0 或 1", field=metric_path)
            if metric.kind == "count":
                if number != number.to_integral_value():
                    raise ValidationError(f"{metric_path} 必须是非负整数", field=metric_path)
                if number < 0:
                    raise ValidationError(f"{metric_path} 计数不能为负值", field=metric_path)
            parsed[key] = number
        return cls(
            source_batch=_required_text(data.get("source_batch"), f"{base}.source_batch"),
            source_row=_required_text(data.get("source_row"), f"{base}.source_row"),
            robot_id=_required_text(data.get("robot_id"), f"{base}.robot_id"),
            protocol_id=protocol_id,
            protocol_version=protocol.version,
            stratum_key=stratum_key,
            observed_at=_timestamp(data.get("observed_at"), f"{base}.observed_at"),
            metrics=parsed,
            excluded_reason=_optional_text(data.get("excluded_reason"), f"{base}.excluded_reason"),
        )
