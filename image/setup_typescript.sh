#!/bin/bash
set -e

# node is copied from the official Node image in the Dockerfile. npm/npx/corepack
# are normally symlinks into lib/node_modules that resolve relative requires from
# their real location; `docker COPY` dereferences symlinks into flat file copies,
# which breaks that resolution, so the links are recreated here instead.
ln -sf /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm
ln -sf /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
ln -sf /usr/local/lib/node_modules/corepack/dist/corepack.js /usr/local/bin/corepack
ln -sf /usr/local/bin/node /usr/local/bin/nodejs

# Install TypeScript and VS Code extension dev tools
npm install -g \
    typescript \
    ts-node \
    yo \
    generator-code \
    eslint \
    @vscode/vsce

# Codex and Claude Code: each official npm package carries the same native
# binary as its standalone installer, as a per-platform optional dependency.
# Claude Code's postinstall links that binary over a stub bin/claude.exe;
# npm 11 still runs it but warns that unlisted install scripts will be
# skipped, so allow it by name.
npm install -g --allow-scripts=@anthropic-ai/claude-code \
    "@openai/codex@${CODEX_VERSION:-latest}" \
    "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION:-latest}"
