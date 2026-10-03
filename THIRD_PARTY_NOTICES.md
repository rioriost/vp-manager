# Third-party components

The MIT license in `LICENSE` applies to vp-manager's original Python code,
documentation, and bundled Skill. It does not relicense dependencies,
VOICEPEAK, voice assets, fonts, or speech recognition models.

The Homebrew formula installs each Python dependency from a separately
checksummed upstream wheel. Those distributions retain their own metadata,
copyright notices, and license files under the formula's virtual environment
(`libexec/lib/python3.12/site-packages`). The complete pinned dependency set
and source URLs are recorded in `uv.lock` and the generated formula.

- NumPy and SoundFile retain their upstream licenses and notices.
- MLX Whisper and its transitive dependencies, including MLX, PyTorch, and
  scientific Python libraries, retain their upstream licenses and notices.
- Python, libsndfile, FFmpeg, and Poppler are separate Homebrew dependencies.
  Their licenses and source packages are described by their respective
  Homebrew formulae. vp-manager invokes FFmpeg and Poppler as subprocesses.
- PyMuPDF is used only to create and inspect development test fixtures. It is
  not a runtime dependency and is not included in the Homebrew Python bundle.
- VOICEPEAK, licensed voice assets, fonts, and ASR model weights are not
  included in this repository or release. Install or obtain them separately
  under their respective terms.

Upstream references: [NumPy](https://numpy.org/doc/stable/license.html),
[SoundFile](https://github.com/bastibe/python-soundfile),
[MLX Whisper](https://github.com/ml-explore/mlx-examples/tree/main/whisper),
[Homebrew formulae](https://formulae.brew.sh/).

## Supplemental MIT notices

The upstream `mlx-whisper` 0.4.3 wheel declares MIT in its metadata but does
not contain the full license text. The following upstream notices supplement
that distribution; the dependency wheel and its copyright headers remain
unchanged.

## MLX Examples / MLX Whisper

Source: https://raw.githubusercontent.com/ml-explore/mlx-examples/main/LICENSE

```text
MIT License

Copyright © 2023 Apple Inc.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## OpenAI Whisper

Source: https://raw.githubusercontent.com/openai/whisper/main/LICENSE

```text
MIT License

Copyright (c) 2022 OpenAI

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
