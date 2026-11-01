"""Real GPU benchmark command for the recorded-video model stack.

The benchmark is intentionally a command module rather than an API route.  It
decodes an uploaded recording, runs the configured detector and tracker on
real frames, runs local SigLIP2/BGE-M3 encoders when their checkpoints are
declared, and invokes a local Qwen responder when configured.  Each stage
reports measured latency, throughput, and peak CUDA memory.  Missing
checkpoints raise an actionable error; no stage substitutes synthetic output.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import argparse
import json
from pathlib import Path
import statistics
import sys
import time
from typing import Any, Iterable, Mapping

from .model_adapters import (
    EmbeddingService,
    ModelSettings,
    ModelUnavailableError,
    QwenGroundedResponder,
    SigLIP2EmbeddingService,
    build_detector,
    build_tracker,
    settings_from_env,
)
from .model_manifest import ManifestError, ModelManifest, ModelSpec, load_model_manifest, validate_local_checkpoints
from .pipeline import PipelineError


class BenchmarkError(RuntimeError):
    """Raised when a real benchmark cannot be executed."""


@dataclass(frozen=True, slots=True)
class BenchmarkStage:
    name: str
    status: str
    samples: int = 0
    elapsed_seconds: float = 0.0
    mean_latency_ms: float | None = None
    p50_latency_ms: float | None = None
    p95_latency_ms: float | None = None
    throughput_per_second: float | None = None
    peak_vram_bytes: int = 0
    model_id: str | None = None
    model_version: str | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["latency_ms"] = self.mean_latency_ms
        result["throughput_fps"] = self.throughput_per_second
        result["peak_vram_mb"] = self.peak_vram_bytes / (1024 * 1024)
        return result


@dataclass(frozen=True, slots=True)
class BenchmarkReport:
    video: str
    manifest: str
    device: str
    frame_count: int
    elapsed_seconds: float
    peak_vram_bytes: int
    stages: tuple[BenchmarkStage, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "video": self.video,
            "manifest": self.manifest,
            "device": self.device,
            "frame_count": self.frame_count,
            "elapsed_seconds": self.elapsed_seconds,
            "peak_vram_bytes": self.peak_vram_bytes,
            "stages": [stage.as_dict() for stage in self.stages],
            "metadata": dict(self.metadata),
        }


def _load_cuda():
    try:
        import torch
    except ImportError as exc:
        raise BenchmarkError("GPU benchmark requires the PyTorch runtime") from exc
    if not torch.cuda.is_available():
        raise BenchmarkError("GPU benchmark requested but CUDA is unavailable")
    return torch


def _sync(torch) -> None:
    torch.cuda.synchronize()


def _peak(torch) -> int:
    return int(torch.cuda.max_memory_allocated())


def _stats(latencies: list[float], elapsed: float, samples: int, peak_vram: int, **kwargs: Any) -> BenchmarkStage:
    milliseconds = [value * 1000 for value in latencies]
    return BenchmarkStage(
        elapsed_seconds=elapsed,
        samples=samples,
        mean_latency_ms=statistics.fmean(milliseconds) if milliseconds else None,
        p50_latency_ms=statistics.median(milliseconds) if milliseconds else None,
        p95_latency_ms=(sorted(milliseconds)[max(0, int(len(milliseconds) * 0.95) - 1)] if milliseconds else None),
        throughput_per_second=(samples / elapsed if elapsed > 0 else None),
        peak_vram_bytes=peak_vram,
        **kwargs,
    )


def _find_spec(manifest: ModelManifest, ids: tuple[str, ...], kinds: tuple[str, ...]) -> ModelSpec | None:
    wanted_ids = {value.lower() for value in ids}
    wanted_kinds = {value.lower() for value in kinds}
    for spec in manifest.models:
        if spec.id.lower() in wanted_ids:
            return spec
    for spec in manifest.models:
        if spec.kind.lower() in wanted_kinds:
            return spec
    return None


def _settings_for_manifest(manifest: ModelManifest, base: ModelSettings, *, detector_spec: ModelSpec | None, visual_spec: ModelSpec | None, text_spec: ModelSpec | None, chat_spec: ModelSpec | None) -> ModelSettings:
    return ModelSettings(
        profile=base.profile,
        device="gpu",
        yolo_model=str(detector_spec.checkpoint) if detector_spec and detector_spec.checkpoint else base.yolo_model,
        siglip_model=str(visual_spec.checkpoint) if visual_spec and visual_spec.checkpoint else base.siglip_model,
        bge_model=str(text_spec.checkpoint) if text_spec and text_spec.checkpoint else base.bge_model,
        qwen_model=str(chat_spec.checkpoint) if chat_spec and chat_spec.checkpoint else base.qwen_model,
        model_cache=base.model_cache,
    )


def _stage_unavailable(name: str, reason: str, *, model: ModelSpec | None = None) -> BenchmarkStage:
    return BenchmarkStage(
        name=name,
        status="unavailable",
        model_id=model.id if model else None,
        model_version=model.version if model else None,
        error=reason,
    )


def run_gpu_benchmark(
    video_path: str | Path,
    manifest_path: str | Path,
    *,
    detector_profile: str = "yolo",
    tracker_profile: str = "bytetrack",
    every: int = 1,
    max_frames: int | None = None,
    embedding_every: int = 10,
    strict: bool = True,
    model_settings: ModelSettings | None = None,
) -> BenchmarkReport:
    """Run the full real stack on a recording and return measured statistics.

    ``strict=True`` (the CLI default) fails if a requested model is absent or
    its local checkpoint is missing.  Set ``strict=False`` only when producing
    a capability report that explicitly records unavailable optional stages.
    """

    video = Path(video_path).expanduser().resolve()
    if not video.is_file():
        raise BenchmarkError(f"uploaded recording does not exist: {video}")
    if every < 1 or embedding_every < 1:
        raise BenchmarkError("every and embedding_every must be positive integers")
    if max_frames is not None and max_frames < 1:
        raise BenchmarkError("max_frames must be positive when provided")
    try:
        manifest = load_model_manifest(manifest_path)
    except ManifestError as exc:
        raise BenchmarkError(str(exc)) from exc

    detector_ids = ("yolo-open-vocabulary", "yolo") if detector_profile.lower() == "yolo" else ("bright-blob-baseline", "baseline")
    detector_spec = _find_spec(manifest, detector_ids, ("detector",))
    visual_spec = _find_spec(manifest, ("siglip2",), ("visual-embedding", "visual_embedding", "image-embedding"))
    text_spec = _find_spec(manifest, ("bge-m3",), ("text-embedding", "text_embedding"))
    chat_spec = _find_spec(manifest, ("qwen3-8b", "qwen"), ("query-and-response-model", "chat"))

    # A selected full-stack component must have a local checkpoint.  Optional
    # stages with no declaration are recorded as unavailable when non-strict.
    requested_specs: list[tuple[str, ModelSpec | None]] = []
    if detector_profile.lower() == "yolo":
        requested_specs.append(("detector", detector_spec))
    if visual_spec is not None:
        requested_specs.append(("siglip2", visual_spec))
    if text_spec is not None:
        requested_specs.append(("bge-m3", text_spec))
    if chat_spec is not None:
        requested_specs.append(("chat", chat_spec))
    for stage_name, spec in requested_specs:
        if spec is None:
            if strict and stage_name == "detector":
                raise BenchmarkError(f"{stage_name} model is not declared in the local manifest")
            continue
        if not any(device in {"cuda", "gpu"} for device in spec.devices):
            if strict or stage_name == "detector":
                raise BenchmarkError(
                    f"{stage_name} model '{spec.id}' is not declared compatible with CUDA: {spec.devices}"
                )
            continue
        try:
            validate_local_checkpoints(manifest, model_ids=(spec.id,))
        except ManifestError as exc:
            if strict or stage_name == "detector":
                raise BenchmarkError(f"{stage_name} checkpoint unavailable: {exc}") from exc

    # Validate configuration before checking the host GPU so a missing local
    # checkpoint is still reported clearly on a CPU-only CI machine.
    torch = _load_cuda()

    base_settings = model_settings or settings_from_env()
    settings = _settings_for_manifest(
        manifest,
        base_settings,
        detector_spec=detector_spec,
        visual_spec=visual_spec,
        text_spec=text_spec,
        chat_spec=chat_spec,
    )
    settings = ModelSettings(
        profile=detector_profile,
        device="gpu",
        yolo_model=settings.yolo_model,
        siglip_model=settings.siglip_model,
        bge_model=settings.bge_model,
        qwen_model=settings.qwen_model,
        model_cache=settings.model_cache,
    )
    try:
        detector = build_detector(settings)
    except (ModelUnavailableError, PipelineError, OSError) as exc:
        raise BenchmarkError(f"detector initialization failed: {exc}") from exc
    try:
        tracker = build_tracker(settings, profile=tracker_profile)
    except (ModelUnavailableError, PipelineError, OSError) as exc:
        raise BenchmarkError(f"tracker initialization failed: {exc}") from exc

    visual_encoder: SigLIP2EmbeddingService | None = None
    text_encoder: EmbeddingService | None = None
    qwen: QwenGroundedResponder | None = None
    unavailable: list[BenchmarkStage] = []
    if visual_spec and visual_spec.checkpoint:
        try:
            visual_encoder = SigLIP2EmbeddingService(settings)
        except (ModelUnavailableError, PipelineError, OSError) as exc:
            if strict:
                raise BenchmarkError(f"SigLIP2 initialization failed: {exc}") from exc
            unavailable.append(_stage_unavailable("siglip2", str(exc), model=visual_spec))
    elif visual_spec:
        unavailable.append(_stage_unavailable("siglip2", "manifest has no local checkpoint", model=visual_spec))
    if text_spec and text_spec.checkpoint:
        try:
            text_encoder = EmbeddingService(settings)
        except (ModelUnavailableError, PipelineError, OSError) as exc:
            if strict:
                raise BenchmarkError(f"BGE-M3 initialization failed: {exc}") from exc
            unavailable.append(_stage_unavailable("bge-m3", str(exc), model=text_spec))
    elif text_spec:
        unavailable.append(_stage_unavailable("bge-m3", "manifest has no local checkpoint", model=text_spec))
    if chat_spec and chat_spec.checkpoint:
        try:
            qwen = QwenGroundedResponder(settings)
        except (ModelUnavailableError, PipelineError, OSError) as exc:
            if strict:
                raise BenchmarkError(f"chat model initialization failed: {exc}") from exc
            unavailable.append(_stage_unavailable("chat", str(exc), model=chat_spec))
    elif chat_spec:
        unavailable.append(_stage_unavailable("chat", "manifest has no local checkpoint", model=chat_spec))

    try:
        import cv2
    except ImportError as exc:
        raise BenchmarkError("GPU benchmark requires opencv-python-headless") from exc
    capture = cv2.VideoCapture(str(video))
    if not capture.isOpened():
        raise BenchmarkError(f"unable to open uploaded recording: {video}")

    detector_latencies: list[float] = []
    tracker_latencies: list[float] = []
    embedding_visual_latencies: list[float] = []
    embedding_text_latencies: list[float] = []
    frames_for_embedding: list[Any] = []
    frame_count = 0
    benchmark_started = time.perf_counter()
    detector_peak_vram = 0
    tracker_peak_vram = 0
    try:
        while max_frames is None or frame_count < max_frames:
            ok, frame = capture.read()
            if not ok:
                break
            if frame_count % every != 0:
                frame_count += 1
                continue
            torch.cuda.reset_peak_memory_stats()
            _sync(torch)
            started = time.perf_counter()
            detections = detector.detect(frame)
            _sync(torch)
            detector_latencies.append(time.perf_counter() - started)
            detector_peak_vram = max(detector_peak_vram, _peak(torch))
            torch.cuda.reset_peak_memory_stats()
            _sync(torch)
            started = time.perf_counter()
            tracker.update(detections)
            _sync(torch)
            tracker_latencies.append(time.perf_counter() - started)
            tracker_peak_vram = max(tracker_peak_vram, _peak(torch))
            if visual_encoder is not None and frame_count % embedding_every == 0:
                frames_for_embedding.append(frame)
            frame_count += 1
    finally:
        capture.release()
    total_elapsed = time.perf_counter() - benchmark_started

    stages: list[BenchmarkStage] = []
    stages.append(
        _stats(
            detector_latencies,
            sum(detector_latencies),
            len(detector_latencies),
            detector_peak_vram,
            name="detector",
            status="completed",
            model_id=detector_spec.id if detector_profile.lower() == "yolo" and detector_spec else "bright-blob-baseline",
            model_version=detector_spec.version if detector_profile.lower() == "yolo" and detector_spec else "bundled",
        )
    )
    stages.append(
        _stats(
            tracker_latencies,
            sum(tracker_latencies),
            len(tracker_latencies),
            tracker_peak_vram,
            name="tracker",
            status="completed",
            model_id=tracker_profile,
            model_version="runtime",
        )
    )

    if visual_encoder is not None:
        torch.cuda.reset_peak_memory_stats()
        started_stage = time.perf_counter()
        for frame in frames_for_embedding:
            _sync(torch)
            started = time.perf_counter()
            try:
                visual_encoder.image(frame)
            except (ModelUnavailableError, PipelineError, OSError, RuntimeError) as exc:
                raise BenchmarkError(f"SigLIP2 inference failed: {exc}") from exc
            _sync(torch)
            embedding_visual_latencies.append(time.perf_counter() - started)
        stages.append(
            _stats(
                embedding_visual_latencies,
                time.perf_counter() - started_stage,
                len(embedding_visual_latencies),
                _peak(torch),
                name="siglip2",
                status="completed",
                model_id=visual_spec.id if visual_spec else None,
                model_version=visual_spec.version if visual_spec else None,
            )
        )
    else:
        if not any(stage.name == "siglip2" for stage in unavailable):
            unavailable.append(_stage_unavailable("siglip2", "no local checkpoint declared", model=visual_spec))

    if text_encoder is not None:
        torch.cuda.reset_peak_memory_stats()
        started_stage = time.perf_counter()
        _sync(torch)
        started = time.perf_counter()
        try:
            text_encoder.text("benchmark uploaded recording evidence")
        except (ModelUnavailableError, PipelineError, OSError, RuntimeError) as exc:
            raise BenchmarkError(f"BGE-M3 inference failed: {exc}") from exc
        _sync(torch)
        embedding_text_latencies.append(time.perf_counter() - started)
        stages.append(
            _stats(
                embedding_text_latencies,
                time.perf_counter() - started_stage,
                len(embedding_text_latencies),
                _peak(torch),
                name="bge-m3",
                status="completed",
                model_id=text_spec.id if text_spec else None,
                model_version=text_spec.version if text_spec else None,
            )
        )
    elif not any(stage.name == "bge-m3" for stage in unavailable):
        unavailable.append(_stage_unavailable("bge-m3", "no local checkpoint declared", model=text_spec))

    if qwen is not None:
        torch.cuda.reset_peak_memory_stats()
        started_stage = time.perf_counter()
        _sync(torch)
        started = time.perf_counter()
        try:
            qwen.respond(
                "Summarize the benchmark evidence without inventing events.",
                [{"video": str(video), "frame_count": frame_count}],
            )
        except (ModelUnavailableError, PipelineError, OSError, RuntimeError) as exc:
            raise BenchmarkError(f"chat inference failed: {exc}") from exc
        _sync(torch)
        stages.append(
            _stats(
                [time.perf_counter() - started],
                time.perf_counter() - started_stage,
                1,
                _peak(torch),
                name="chat",
                status="completed",
                model_id=chat_spec.id if chat_spec else None,
                model_version=chat_spec.version if chat_spec else None,
            )
        )
    elif not any(stage.name == "chat" for stage in unavailable):
        unavailable.append(_stage_unavailable("chat", "no local checkpoint declared", model=chat_spec))

    stages.extend(unavailable)
    peak = max((stage.peak_vram_bytes for stage in stages), default=0)
    return BenchmarkReport(
        video=str(video),
        manifest=str(Path(manifest_path).expanduser().resolve()),
        device="cuda",
        frame_count=len(detector_latencies),
        elapsed_seconds=total_elapsed,
        peak_vram_bytes=peak,
        stages=tuple(stages),
        metadata={"every": every, "embedding_every": embedding_every, "strict": strict},
    )


run_full_stack_benchmark = run_gpu_benchmark


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Benchmark the real GPU video intelligence stack")
    parser.add_argument("--video", required=True, help="uploaded recording path")
    parser.add_argument("--manifest", required=True, help="local JSON model manifest")
    parser.add_argument("--detector", choices=("baseline", "yolo"), default="yolo")
    parser.add_argument("--tracker", choices=("centroid", "bytetrack"), default="bytetrack")
    parser.add_argument("--every", type=int, default=1)
    parser.add_argument("--max-frames", type=int)
    parser.add_argument("--embedding-every", type=int, default=10)
    parser.add_argument("--allow-missing-optional", action="store_true", help="report optional stages as unavailable")
    parser.add_argument("--output", type=Path, help="write the JSON report to this path")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    try:
        report = run_gpu_benchmark(
            args.video,
            args.manifest,
            detector_profile=args.detector,
            tracker_profile=args.tracker,
            every=args.every,
            max_frames=args.max_frames,
            embedding_every=args.embedding_every,
            strict=not args.allow_missing_optional,
        )
    except (BenchmarkError, ManifestError, OSError, ValueError) as exc:
        print(f"benchmark failed: {exc}", file=sys.stderr)
        return 2
    document = json.dumps(report.as_dict(), indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(document + "\n", encoding="utf-8")
    print(document)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the CLI
    raise SystemExit(main())


__all__ = ["BenchmarkError", "BenchmarkReport", "BenchmarkStage", "main", "run_full_stack_benchmark", "run_gpu_benchmark"]
