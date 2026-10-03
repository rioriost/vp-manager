# Homebrew release inputs

This tap formula targets CPython 3.12 on Apple Silicon macOS 14 or later.
Its Python dependency closure includes the optional MLX Whisper ASR runtime.
It does not bundle or download speech models or VOICEPEAK assets. Poppler,
FFmpeg, Python and libsndfile are Homebrew dependencies. PyMuPDF is excluded.
Intel macOS and Linux are unsupported by this formula; macOS 14 is the wheel
compatibility floor, not a claim that every OS release has been tested.

After updating the public PyPI `uv.lock` and building the final release wheel:

```sh
uv run --script scripts/prepare_homebrew_resources.py \
  --wheel dist/vp_manager-0.2.0-py3-none-any.whl
```

The generator emits `vp-manager.rb` and `resources-macos-arm64.json`. It rejects
private package-feed URLs, missing compatible wheels and mismatched package
identities. It prefers pure wheels when available so soundfile uses the declared
Homebrew libsndfile. Every resource remains pinned by its lockfile SHA-256.
Use `--check` to verify regeneration, or `--download-dir /absolute/wheelhouse`
to prefetch and verify all resources plus the local release wheel. The latter
does not install anything. Its dependency license metadata remains in each wheel.

Publish exactly the wheel whose hash the generator recorded. Stage the generated
formula in the tap only after checking the GitHub release asset's remote hash.
Homebrew fetches checksummed resources before entering the install sandbox; the
formula then installs wheel files with `PIP_NO_INDEX=1`, `PIP_NO_DEPS=1` and
`build_isolation: false`. No source build or implicit build-backend download is
required. The formula preserves package license files and bundled native notices.
See the project's `THIRD_PARTY_NOTICES.md` for the dependency license overview.

The supported Homebrew `preserve_rpath` setting retains wheel dylib IDs beginning
with `@rpath`. Replacing those short, relocatable IDs with a full Cellar/opt path
can exceed the Mach-O header space reserved by upstream wheels such as tiktoken.
This setting preserves those IDs while Homebrew still performs its other linkage
checks; it does not disable dynamic linkage handling wholesale.

For Homebrew style validation, copy the generated file into a temporary
`Formula/` directory and use `brew style --except-cops
FormulaAudit/ResourceRequiresDependencies /path/Formula/vp-manager.rb`.
This one audit exception concerns PyYAML's pinned binary wheel, which already
embeds libyaml. Declaring an unused external libyaml dependency does not change
that wheel. Homebrew prohibits inline cop-disable directives in formulae, so the
exception is passed explicitly to the check rather than embedded as a directive.

The formula test uses a synthetic WAV, an isolated Skill dry run and text
analysis. It never starts VOICEPEAK or downloads a model. A passing formula test
does not validate pronunciation, model quality, live synthesis or slide fidelity.

Reference: [Homebrew language-specific formula guidance](https://docs.brew.sh/Language-Specific-Formulae).
