#!/usr/bin/env node
/**
 * Exports a flat, single-package Vite + React + TypeScript bundle of the SyrinxOne Web UI
 * ready for Google AI Studio Build (no npm workspaces required).
 *
 * Usage:
 *   node scripts/export-ai-studio.mjs [--out dist-ai-studio] [--api https://...]
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const repoRoot = path.resolve(__dirname, '..');

const args = process.argv.slice(2);
function getArg(flag, fallback) {
  const idx = args.indexOf(flag);
  return idx >= 0 && args[idx + 1] ? args[idx + 1] : fallback;
}

const outDir = path.resolve(repoRoot, getArg('--out', 'dist-ai-studio'));
const apiUrl = getArg(
  '--api',
  process.env.VITE_API_URL || 'https://syrinx-engine-466330924576.us-west1.run.app',
);

const webDir = path.join(repoRoot, 'apps', 'web');
const sharedSrcDir = path.join(repoRoot, 'packages', 'shared', 'src');

function copyDirRecursive(src, dst) {
  fs.mkdirSync(dst, { recursive: true });
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    const srcPath = path.join(src, entry.name);
    const dstPath = path.join(dst, entry.name);
    if (entry.isDirectory()) {
      copyDirRecursive(srcPath, dstPath);
    } else if (entry.isFile()) {
      // Skip vitest test files in the AI Studio UI bundle
      if (entry.name.endsWith('.test.ts') || entry.name.endsWith('.test.tsx')) {
        continue;
      }
      fs.copyFileSync(srcPath, dstPath);
    }
  }
}

// Clean output directory
fs.rmSync(outDir, { recursive: true, force: true });
fs.mkdirSync(outDir, { recursive: true });

// 1. Copy web HTML + src + inline shared src into src/shared
fs.copyFileSync(path.join(webDir, 'index.html'), path.join(outDir, 'index.html'));
copyDirRecursive(path.join(webDir, 'src'), path.join(outDir, 'src'));
copyDirRecursive(sharedSrcDir, path.join(outDir, 'src', 'shared'));

// 2. Write flat standalone package.json (inherits versions from apps/web/package.json)
const webPkg = JSON.parse(fs.readFileSync(path.join(webDir, 'package.json'), 'utf8'));
const flatDeps = { ...webPkg.dependencies };
delete flatDeps['@syrinxone/shared'];

const flatDevDeps = { ...webPkg.devDependencies };
delete flatDevDeps['vitest'];

const standalonePkg = {
  name: 'syrinxone-web-ai-studio',
  version: webPkg.version || '0.1.0',
  private: true,
  type: 'module',
  scripts: {
    dev: 'vite --port 3000 --host 0.0.0.0',
    build: 'vite build',
    preview: 'vite preview --port 4173',
    typecheck: 'tsc --noEmit',
  },
  dependencies: flatDeps,
  devDependencies: flatDevDeps,
};
fs.writeFileSync(
  path.join(outDir, 'package.json'),
  JSON.stringify(standalonePkg, null, 2) + '\n',
  'utf8',
);

// 3. Write vite.config.ts mapping @syrinxone/shared -> ./src/shared/index.ts
// Keeping the exact @syrinxone/shared specifier makes syncing edited files back to apps/web/src 1:1.
const viteConfig = `import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  plugins: [react()],
  base: './',
  resolve: {
    alias: {
      '@syrinxone/shared': path.resolve(__dirname, 'src/shared/index.ts'),
    },
  },
  server: {
    port: 3000,
    host: '0.0.0.0',
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
});
`;
fs.writeFileSync(path.join(outDir, 'vite.config.ts'), viteConfig, 'utf8');

// 4. Write tsconfig.json with matching path alias
const tsconfig = {
  compilerOptions: {
    target: 'ES2022',
    useDefineForClassFields: true,
    lib: ['ES2022', 'DOM', 'DOM.Iterable'],
    module: 'ESNext',
    moduleResolution: 'Bundler',
    jsx: 'react-jsx',
    strict: true,
    noEmit: true,
    skipLibCheck: true,
    esModuleInterop: true,
    resolveJsonModule: true,
    isolatedModules: true,
    baseUrl: '.',
    paths: {
      '@syrinxone/shared': ['./src/shared/index.ts'],
    },
  },
  include: ['src'],
};
fs.writeFileSync(
  path.join(outDir, 'tsconfig.json'),
  JSON.stringify(tsconfig, null, 2) + '\n',
  'utf8',
);

// 5. Pre-wire live Cloud Run backend URL in .env
fs.writeFileSync(path.join(outDir, '.env'), `VITE_API_URL=${apiUrl}\n`, 'utf8');

// 6. Google AI Studio Build metadata.json
const metadata = {
  name: 'SyrinxOne Bioacoustic Studio',
  description:
    'WebGL2 bioacoustic spectrogram viewer, measurement & annotation workstation, and AI Explorer connected to the SyrinxOne Cloud Run DSP & Acoustic Archive engine (MBARI, NOAA NCEI, Google Drive).',
  requestFramePermissions: [],
};
fs.writeFileSync(
  path.join(outDir, 'metadata.json'),
  JSON.stringify(metadata, null, 2) + '\n',
  'utf8',
);

// 7. Standalone .gitignore and README.md
fs.writeFileSync(path.join(outDir, '.gitignore'), 'node_modules/\ndist/\n.vite/\n*.log\n', 'utf8');

const readme = `# SyrinxOne Web UI — Google AI Studio Build Bundle

This is a flat, standalone Vite + React + TypeScript export of \`apps/web\` (with \`packages/shared/src\` inlined at \`src/shared/\`) so it runs directly inside **Google AI Studio Build** with zero npm workspace setup.

## Connected DSP Engine
Pre-configured in \`.env\`:
\`\`\`
VITE_API_URL=${apiUrl}
\`\`\`
You can also switch the backend URL (or add a Bearer token) at runtime by clicking the **S1 SyrinxOne** badge in the top-left toolbar, or opening \`?api=https://...\` in the browser.

## Local Preview
\`\`\`bash
npm install
npm run dev
\`\`\`

## Syncing Changes Back to the Main Monorepo
Because \`vite.config.ts\` aliases \`@syrinxone/shared\` to \`src/shared/index.ts\`, all imports stay identical to \`apps/web/src\`. Copy modified files from \`src/\` (excluding \`src/shared/\`) directly back into \`apps/web/src/\` in the main \`master\` branch.
`;
fs.writeFileSync(path.join(outDir, 'README.md'), readme, 'utf8');

console.log(`Exported Google AI Studio Build bundle to: ${outDir}`);
console.log(`Configured VITE_API_URL: ${apiUrl}`);
