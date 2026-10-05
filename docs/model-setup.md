# Model Setup

Configure access separately on every computer. Never copy authentication files or API keys into this repository.

## Signed-In Local Runtime

The app requests exactly `gpt-6-astra` with low reasoning effort. Version **0.153.4** of the OpenAI Codex CLI was used for the existing live studio. Older versions may reject Astra. Installing a runtime does not grant model access; your signed-in account must have access to that model.

If a compatible CLI is already installed, sign in on this computer, then run the studio normally. The app looks for `codex` on `PATH` or for the executable named by `ASTRA_RUNTIME_BIN`. See the [official CLI installation and sign-in guide](https://learn.chatgpt.com/docs/codex/cli).

To isolate the tested runtime from an existing installation, use Node.js/npm:

```bash
npm install --prefix "$HOME/.local/share/astra-runtime" @openai/codex@0.153.4
export ASTRA_RUNTIME_BIN="$HOME/.local/share/astra-runtime/node_modules/.bin/codex"
"$ASTRA_RUNTIME_BIN" --version
"$ASTRA_RUNTIME_BIN" login
"$ASTRA_RUNTIME_BIN" login status
```

Complete the sign-in in your own browser. Re-run the `export ASTRA_RUNTIME_BIN=...` line in each new terminal before starting the app, or configure that non-secret path in your shell profile. Do not copy another machine's authentication directory.

The studio runs isolated, ephemeral requests with tools disabled. Your prompt, or the captured JPEG plus prompt, is sent to the model when you choose Draw. Local mode means local application hosting and authentication, not offline model inference.

## API Backend

Alternatively, provide `OPENAI_API_KEY` through the server's environment using your normal secret manager, and run:

```bash
python scripts/astra_studio.py --backend api --port 8781
```

Add `--enable-plotter` only after completing the separate hardware setup. Do not put keys in source files, URLs, browser code, GitHub, or command examples that will enter shell history.

API mode uses OpenAI's Responses endpoint, requests `gpt-6-astra`, and disables response storage with `store: false`. An API account with access to that exact model and sufficient quota is required; a ChatGPT subscription does not by itself verify API entitlement. The API transport has regression coverage but has not been live-validated for this standalone package.

## When Generation Fails

Check the runtime version, sign-in, network connection, and account access. Local-mode error details are stored in ignored `outputs/astra/<run-id>/runtime_error.json`. Do not publish these files without reviewing them. Failures are shown as failures; no replacement model or synthetic drawing is used.
