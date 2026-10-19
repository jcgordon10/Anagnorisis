"""Descriptor backend that talks to a llama.cpp server sidecar.

Same worker-visible interface as the in-process transformers backend in
:mod:`anagnorisis_core.models.descriptor` (``initiate`` plus the
``describe_*`` entry points), so the proxy, callers, caches and task code
need no changes: ``_worker_loop`` picks this class when
``cfg.omni.backend == 'llamacpp'``.

Media travels as base64 data URIs / parts in an OpenAI-style
``/v1/chat/completions`` call — no shared filesystem with the sidecar:

* text      → plain user message (truncated to ``max_text_chars``)
* image     → ``image_url`` (resized to ``image_max_size``, JPEG)
* audio     → ``input_audio`` (16 kHz mono WAV, exact 30 s windows — the
  same fixed-window rule as the transformers backend)
* video     → 4 ``image_url`` stills + the window's ``input_audio`` per
  window (caller-extracted frames; needs no ffmpeg in the sidecar)

Long audio/video are windowed and synthesised with the same segment
recipe as the transformers backend. Prompt strings, the refusal vetting
and the cache-key philosophy are shared with it deliberately: the
sidecar runs the same model family (Gemma 4), so the tuning transfers.
"""

import base64
import hashlib
import io
import os
import time

import anagnorisis_core.storage.virtual_file_system as vfs
from anagnorisis_core.models.descriptor import (
    AUDIO_WINDOW_SECONDS,
    _OmniDescriptorImpl,
    fit_audio_window,
    looks_like_a_refusal,
)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode('ascii')


def _image_to_jpeg_bytes(image, max_size: int) -> bytes:
    if max(image.size) > max_size:
        scale = max_size / max(image.size)
        image = image.resize(
            (int(image.width * scale), int(image.height * scale)),
        )
    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="JPEG", quality=88)
    return buf.getvalue()


def _wav_bytes(waveform, sample_rate: int = 16000) -> bytes:
    """16-bit PCM WAV bytes for a mono float waveform."""
    import numpy as np
    import soundfile as sf

    clip = np.asarray(waveform, dtype=np.float32).ravel()
    buf = io.BytesIO()
    sf.write(buf, clip, sample_rate, format='WAV', subtype='PCM_16')
    return buf.getvalue()


class _LlamaCppDescriptorImpl:
    """HTTP twin of ``_OmniDescriptorImpl``; runs inside the same worker."""

    def __init__(self, cfg):
        self.cfg = cfg
        omni = cfg.omni.get('llamacpp', {}) if hasattr(cfg, 'omni') else {}
        self.endpoint = (os.environ.get('ANAGNORISIS_LLAMACPP_ENDPOINT')
                         or omni.get('endpoint', 'http://llamacpp:8080')).rstrip('/')
        self.timeout = int(omni.get('timeout_seconds', 600))
        self.model_label = omni.get(
            'model', 'unsloth/gemma-4-E4B-it-qat-GGUF:UD-Q4_K_XL')
        self.max_text_chars = int(omni.get('max_text_chars', 12000))
        # A plain namespace: the worker loop only reads ``device.type``.
        import types
        self.device = types.SimpleNamespace(type='remote')
        self.model_hash = None
        self._session = None

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def initiate(self, models_folder: str):
        import requests

        self._session = requests.Session()
        try:
            r = self._session.get(f"{self.endpoint}/health", timeout=15)
            r.raise_for_status()
        except Exception as exc:
            raise RuntimeError(
                f"LlamaCppDescriptor: sidecar not reachable at "
                f"{self.endpoint}/health: {exc}") from exc
        self.model_hash = self._calculate_model_hash()
        print(f"LlamaCppDescriptor (Worker): using sidecar at {self.endpoint} "
              f"({self.model_label}).")

    def _calculate_model_hash(self) -> str:
        """Cache identity: backend + model label + everything that changes output."""
        omni = self.cfg.omni
        md5 = hashlib.md5()
        for setting in (
            'llamacpp',
            self.model_label,
            omni.get('max_new_tokens', 256),
            omni.get('segment_max_new_tokens', 96),
            omni.get('do_sample', False),
            omni.get('temperature', 0.3),
            omni.get('image_prompt', ''),
            omni.get('audio_prompt', ''),
            omni.get('video_prompt', ''),
            omni.get('text_prompt', ''),
            self.max_text_chars,
        ):
            md5.update(str(setting).encode('utf-8'))
        return md5.hexdigest()

    # ------------------------------------------------------------------
    # Transport — the single place this file talks to the sidecar
    # ------------------------------------------------------------------

    def _chat(self, content_parts, max_new_tokens=None) -> str:
        """One chat turn; ``content_parts`` are OpenAI-style content blocks."""
        if self._session is None:
            raise RuntimeError("LlamaCppDescriptor not initiated.")
        omni = self.cfg.omni
        try:
            llamacpp_cfg = omni.get('llamacpp', {})
        except Exception:
            llamacpp_cfg = {}
        multiplier = int(llamacpp_cfg.get('token_budget_multiplier', 3)
                         if hasattr(llamacpp_cfg, 'get') else 3)
        base_tokens = max_new_tokens or omni.get('max_new_tokens', 256)
<<<<<<< HEAD
        model_alias = (llamacpp_cfg.get('model_alias', 'gemma-e4b')
                       if hasattr(llamacpp_cfg, 'get') else 'gemma-e4b')
        payload = {
            "model": model_alias,  # llama-swap routes (and lazy-loads) on this
=======
        payload = {
>>>>>>> origin/main
            "messages": [{"role": "user", "content": content_parts}],
            "max_tokens": base_tokens * multiplier,
            "stream": False,
            "enable_thinking": False,
        }
        if omni.get('do_sample', False):
            payload["temperature"] = omni.get('temperature', 0.3)
        try:
            r = self._session.post(
                f"{self.endpoint}/v1/chat/completions",
                json=payload, timeout=self.timeout)
            r.raise_for_status()
            text = (r.json()["choices"][0]["message"].get("content") or "").strip()
        except Exception as exc:
            raise RuntimeError(f"LlamaCppDescriptor: chat call failed: {exc}") from exc
        if looks_like_a_refusal(text):
            print(f"  Discarded a description the model wrote without the media: "
                  f"{text[:80]}…")
            return ''
        return text

    def _prompt_for(self, kind: str) -> str:
        key = f'{kind}_prompt'
        prompt = self.cfg.omni.get(key, None)
        if not prompt:
            raise ValueError(f"Prompt not specified in config (cfg.omni.{key}).")
        return prompt

    # ------------------------------------------------------------------
    # Shared sampling helpers (same recipe as the transformers backend)
    # ------------------------------------------------------------------

    def _synthesise(self, descriptions, total_s: float, media: str) -> str:
        if not descriptions:
            raise RuntimeError(f"Failed to describe any {media} segments.")
        if len(descriptions) == 1:
            return descriptions[0].split(": ", 1)[-1]
        joined = "\n".join(descriptions)
        return self._chat([{
            "type": "text",
            "text": (
                f"The following are descriptions of {len(descriptions)} sampled "
                f"segments from a {media} recording that is {total_s:.0f} seconds "
                f"long:\n\n{joined}\n\n"
                f"Write a concise description of the {media} as a whole. Write only "
                f"the description itself — no preamble, and no mention of samples, "
                f"segments or timestamps."),
        }])

    def _describe_segments(self, segments, media: str, prompt: str, total_s: float) -> str:
        """Describe each pre-built segment, tolerate failures, then synthesise."""
        segment_tokens = self.cfg.omni.get('segment_max_new_tokens', 96)
        descriptions = []
        for idx, (start_s, end_s, parts) in enumerate(segments):
            print(f"LlamaCppDescriptor (Worker): {media} segment {idx + 1} "
                  f"[{start_s:.1f}s – {end_s:.1f}s] …")
            try:
                text_part = {"type": "text", "text": prompt}
                desc = self._chat([*parts, text_part], max_new_tokens=segment_tokens)
                if not desc:
                    print(f"  Segment {idx + 1} produced no usable description.")
                    continue
                descriptions.append(f"[{start_s:.0f}s–{end_s:.0f}s]: {desc[:1024].strip()}")
            except Exception as exc:
                print(f"  Error on segment {idx + 1}: {exc}")
        return self._synthesise(descriptions, total_s, media)

    # ------------------------------------------------------------------
    # Description entry points (same names/signatures as transformers impl)
    # ------------------------------------------------------------------

    def describe_image(self, image_path: str, prompt=None) -> str:
        from PIL import Image

        prompt = prompt or self._prompt_for('image')
        local_path, temp_to_cleanup = vfs.resolve_to_local_path(image_path)
        if not os.path.exists(local_path):
            raise ValueError(f"[LlamaCppDescriptor] [Error: file not found — {image_path}]")
        try:
            image = Image.open(local_path).convert("RGB")
            max_size = self.cfg.omni.get('image_max_size', 512)
            blob = _image_to_jpeg_bytes(image, max_size)
            return self._chat([
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{_b64(blob)}"}},
                {"type": "text", "text": prompt},
            ])
        finally:
            if temp_to_cleanup:
                try:
                    os.remove(temp_to_cleanup)
                except OSError:
                    pass

    def describe_audio(self, audio_path: str, prompt=None) -> str:
        """Describe an audio file short enough to be heard in one turn."""
        import librosa

        prompt = prompt or self._prompt_for('audio')
        local_path, temp_to_cleanup = vfs.resolve_to_local_path(audio_path)
        if not os.path.exists(local_path):
            raise ValueError(f"[LlamaCppDescriptor] [Error: file not found — {audio_path}]")
        try:
            waveform, _ = librosa.load(local_path, sr=16000, mono=True,
                                       duration=AUDIO_WINDOW_SECONDS)
            clip = fit_audio_window(waveform)
            return self._chat([
                {"type": "input_audio",
                 "input_audio": {"data": _b64(_wav_bytes(clip)), "format": "wav"}},
                {"type": "text", "text": prompt},
            ])
        finally:
            if temp_to_cleanup:
                try:
                    os.remove(temp_to_cleanup)
                except OSError:
                    pass

    def describe_audio_sampled(self, audio_path: str, n_samples: int = 3,
                               sample_duration_s: float = AUDIO_WINDOW_SECONDS,
                               prompt=None) -> str:
        import librosa

        prompt = prompt or self._prompt_for('audio')
        sample_duration_s = min(sample_duration_s, AUDIO_WINDOW_SECONDS)
        local_path, temp_to_cleanup = vfs.resolve_to_local_path(audio_path)
        if not os.path.exists(local_path):
            raise ValueError(f"[LlamaCppDescriptor] [Error: file not found — {audio_path}]")
        try:
            total_s = float(librosa.get_duration(path=local_path))
            if total_s <= AUDIO_WINDOW_SECONDS:
                waveform, _ = librosa.load(local_path, sr=16000, mono=True,
                                           duration=AUDIO_WINDOW_SECONDS)
                clip = fit_audio_window(waveform)
                return self._chat([
                    {"type": "input_audio",
                     "input_audio": {"data": _b64(_wav_bytes(clip)), "format": "wav"}},
                    {"type": "text", "text": prompt},
                ])

            segments = []
            for start_s in _OmniDescriptorImpl._segment_starts(
                    total_s, n_samples, sample_duration_s):
                clip, _ = librosa.load(local_path, sr=16000, mono=True,
                                        offset=start_s, duration=sample_duration_s)
                if not len(clip):
                    continue
                clip = fit_audio_window(clip)
                parts = [{"type": "input_audio",
                          "input_audio": {"data": _b64(_wav_bytes(clip)),
                                          "format": "wav"}}]
                segments.append((start_s, start_s + len(clip) / 16000, parts))

            if not segments:
                raise RuntimeError("Failed to read any audio window.")
            if len(segments) == 1:
                _, _, parts = segments[0]
                return self._chat([*parts, {"type": "text", "text": prompt}])
            return self._describe_segments(segments, 'audio', prompt, total_s)
        finally:
            if temp_to_cleanup:
                try:
                    os.remove(temp_to_cleanup)
                except OSError:
                    pass

    def describe_video_sampled(self, video_path: str, n_samples: int = 3,
                               sample_duration_s: float = 10.0,
                               frames_per_segment: int = 4,
                               prompt=None) -> str:
        """Describe a video from windows of stills + the sound underneath.

        Frames are caller-extracted (no ffmpeg needed in the sidecar):
        ``frames_per_segment`` stills plus the window's soundtrack per turn.
        """
        import cv2
        import librosa
        from PIL import Image

        prompt = prompt or self._prompt_for('video')
        sample_duration_s = min(sample_duration_s, AUDIO_WINDOW_SECONDS)
        frames_per_segment = int(
            self.cfg.omni.get('video_frames_per_window', frames_per_segment))
        local_path, temp_to_cleanup = vfs.resolve_to_local_path(video_path)
        if not os.path.exists(local_path):
            raise ValueError(f"[LlamaCppDescriptor] [Error: file not found — {video_path}]")
        try:
            cap = cv2.VideoCapture(local_path)
            if not cap.isOpened():
                raise RuntimeError(f"Cannot open video: {local_path}")
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
            cap.release()
            total_s = total_frames / fps if fps else 0.0
            if total_s <= 0:
                raise RuntimeError(f"Could not determine duration of: {video_path}")

            max_size = self.cfg.omni.get('video_sample_max_size', 512)
            segments = []
            for start_s in _OmniDescriptorImpl._segment_starts(
                    total_s, n_samples, sample_duration_s):
                end_s = min(start_s + sample_duration_s, total_s)
                frames = self._grab_frames(local_path, start_s, end_s,
                                           frames_per_segment, fps, max_size)
                if not frames:
                    print(f"  No frames extracted at {start_s:.1f}s — skipping segment.")
                    continue
                clip, _ = librosa.load(local_path, sr=16000, mono=True,
                                        offset=start_s,
                                        duration=end_s - start_s)
                parts = [
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{_b64(f)}"}}
                    for f in frames
                ]
                if len(clip):
                    clip = fit_audio_window(clip)
                    parts.append(
                        {"type": "input_audio",
                         "input_audio": {"data": _b64(_wav_bytes(clip)),
                                         "format": "wav"}})
                segments.append((start_s, end_s, parts))

            if not segments:
                raise RuntimeError(f"Could not extract any frames from: {video_path}")
            if len(segments) == 1:
                _, _, parts = segments[0]
                return self._chat([*parts, {"type": "text", "text": prompt}])
            return self._describe_segments(segments, 'video', prompt, total_s)
        finally:
            if temp_to_cleanup:
                try:
                    os.remove(temp_to_cleanup)
                except OSError:
                    pass

    @staticmethod
    def _grab_frames(local_path, start_s, end_s, count, fps, max_size):
        """Evenly spaced JPEG bytes across one window."""
        import cv2

        cap = cv2.VideoCapture(local_path)
        try:
            out = []
            span = max(end_s - start_s, 1.0 / max(fps, 1.0))
            from PIL import Image
            for i in range(count):
                t = start_s if count == 1 else start_s + span * i / (count - 1)
                cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
                ok, frame = cap.read()
                if not ok or frame is None:
                    continue
                image = Image.fromarray(
                    cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                out.append(_image_to_jpeg_bytes(image, max_size))
            return out
        finally:
            cap.release()

    def describe_text(self, text: str, prompt=None) -> str:
        """Summarise a long text (already truncated upstream if huge)."""
        prompt = prompt or self._prompt_for('text')
        if len(text) > self.max_text_chars:
            text = text[:self.max_text_chars]
        return self._chat([{"type": "text", "text": f"{prompt}\n\n{text}"}])
