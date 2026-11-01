"""Pinned upstream H3 source-fit contract for offline CPU graph tests.

H3 _fit_start/_pixel_frames: seitanism/ComfyUI-H3-Motion-Context-MultiRef
361624fb406b63eb6694442eac6c895fc1533a70, h3_v2v_fractional.py.
Target geometry and core batch/audio operations: Comfy-Org/ComfyUI
ee71d5c4993f29086b27fde1629a945ae48425bf, comfy_extras/nodes_minimax_h3.py,
nodes_images.py and nodes_audio.py. Function names/NodeOutput wrappers and tensor
storage are adapted for NumPy; fitting, geometry and image selection math are
retained. Audio tests exercise the matching-rate branch. No GPU imports.
"""

FRAME_PER_TOKEN = (1, 4, 4, 4, 4)
FPS = 24.0
AUDIO_LATENT_FPS = 40.0


def align_frame_count(n):
    while n % 17 != 5:
        n += 1
    return n


def video_latent_t(frame_count):
    return 2 if frame_count <= 5 else ((frame_count - 5) // 17) * 5 + 2


def temporal_shape(length):
    frame_count = align_frame_count(max(5, length))
    duration = frame_count / FPS
    return frame_count, video_latent_t(frame_count), round(duration * AUDIO_LATENT_FPS)


def pixel_frames(latent_t):
    return sum(FRAME_PER_TOKEN[k % 5] for k in range(int(latent_t)))


def fit_start(available, wanted, mode):
    available = int(available)
    wanted = int(wanted)
    if available < wanted:
        raise ValueError(
            "h3_v2v_fractional: source becomes %d frames at 24 fps but target needs %d"
            % (available, wanted)
        )
    extra = available - wanted
    if mode == "end":
        return extra
    if mode == "center":
        return extra // 2
    return 0


class TinyTensor:
    """NumPy-backed subset of torch.Tensor used by copied upstream methods."""

    def __init__(self, value):
        import numpy as np

        self.value = np.asarray(value)

    @property
    def shape(self):
        return self.value.shape

    def __getitem__(self, key):
        return TinyTensor(self.value[key])

    def clone(self):
        return TinyTensor(self.value.copy())

    def repeat(self, repeats):
        import numpy as np

        return TinyTensor(np.tile(self.value, repeats))


class _Torch:
    @staticmethod
    def cat(values, dim=0):
        import numpy as np

        return np.concatenate([value.value if isinstance(value, TinyTensor) else value
                               for value in values], axis=dim)

    @staticmethod
    def zeros(shape, dtype=None):
        import numpy as np

        return np.zeros(shape, dtype=dtype)


torch = _Torch()


def empty_audio_execute(duration, sample_rate, channels):
    # EmptyAudio.execute arithmetic, with NumPy storage and unwrapped output.
    import numpy as np

    num_samples = int(round(duration * sample_rate))
    waveform = torch.zeros((1, channels, num_samples), dtype=np.float32)
    return {"waveform": waveform, "sample_rate": sample_rate}


def _match_audio_sample_rates(waveform_1, sample_rate_1, waveform_2, sample_rate_2):
    # Exact same-rate branch from pinned nodes_audio.py; the regression supplies
    # VHS and EmptyAudio at the same rate, so no torchaudio is needed.
    if sample_rate_1 != sample_rate_2:
        raise AssertionError("CPU fixture expects matching audio sample rates")
    return waveform_1, waveform_2, sample_rate_1


def audio_concat_execute(audio1, audio2, direction):
    # AudioConcat.execute matching-rate arithmetic and unwrapped output.
    import numpy as np

    waveform_1 = audio1["waveform"]
    waveform_2 = audio2["waveform"]
    sample_rate_1 = audio1["sample_rate"]
    sample_rate_2 = audio2["sample_rate"]
    if waveform_1.shape[1] == 1:
        waveform_1 = np.repeat(waveform_1, 2, axis=1)
    if waveform_2.shape[1] == 1:
        waveform_2 = np.repeat(waveform_2, 2, axis=1)
    waveform_1, waveform_2, output_sample_rate = _match_audio_sample_rates(
        waveform_1, sample_rate_1, waveform_2, sample_rate_2
    )
    if direction == "after":
        concatenated_audio = torch.cat((waveform_1, waveform_2), dim=2)
    elif direction == "before":
        concatenated_audio = torch.cat((waveform_2, waveform_1), dim=2)
    return {"waveform": concatenated_audio, "sample_rate": output_sample_rate}


def image_from_batch(image, batch_index, length):
    # ImageFromBatch.execute selection math, with unwrapped output.
    s_in = image
    if batch_index < 0:
        batch_index += s_in.shape[0]
    batch_index = max(0, min(s_in.shape[0] - 1, batch_index))
    length = min(s_in.shape[0] - batch_index, length)
    s = s_in[batch_index:batch_index + length].clone()
    return s


def repeat_image_batch(image, amount):
    # RepeatImageBatch.execute, with unwrapped output.
    return image.repeat((amount, 1, 1, 1))
