#!/usr/bin/env node
/**
 * Two-way sync between the SyrinxOne monorepo (apps/web + packages/shared)
 * and the flat companion Google AI Studio Build repo (quill65/SyrinxOne-Web:main).
 *
 * Usage:
 *   node scripts/sync-ai-studio.mjs push [--repo URL] [--branch main] [--message "..."]
 *   node scripts/sync-ai-studio.mjs pull [--repo URL] [--branch main]
 */
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const repoRoot = path.resolve(__dirname, '..');

const args = process.argv.slice(2);
const mode = args[0];

function getArg(flag, fallback) {
  const idx = args.indexOf(flag);
  return idx >= 0 && args[idx + 1] ? args[idx + 1] : fallback;
}

const remoteRepo = getArg('--repo', 'https://github.com/quill65/SyrinxOne-Web.git');
const branch = getArg('--branch', 'main');
const customMessage = getArg('--message', '');

function run(cmd, cmdArgs, opts = {}) {
  return execFileSync(cmd, cmdArgs, {
    stdio: opts.capture ? ['ignore', 'pipe', 'pipe'] : 'inherit',
    encoding: 'utf8',
    cwd: opts.cwd || repoRoot,
  });
}

function listFilesRecursive(dir, baseDir = dir) {
  const results = [];
  if (!fs.existsSync(dir)) return results;
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (entry.name === '.git') continue;
    const full = path.join(dir, entry.name);
    const rel = path.relative(baseDir, full);
    if (entry.isDirectory()) {
      results.push(...listFilesRecursive(full, baseDir));
    } else if (entry.isFile()) {
      results.push(rel);
    }
  }
  return results;
}

function copyDirClean(srcDir, dstDir, { excludeRelPrefixes = [], preserveFilter = null } = {}) {
  const srcFiles = listFilesRecursive(srcDir).filter(
    (rel) => !excludeRelPrefixes.some((p) => rel.replace(/\\/g, '/').startsWith(p)),
  );
  const srcSet = new Set(srcFiles.map((r) => r.replace(/\\/g, '/')));

  let copied = 0;
  for (const rel of srcFiles) {
    const srcPath = path.join(srcDir, rel);
    const dstPath = path.join(dstDir, rel);
    fs.mkdirSync(path.dirname(dstPath), { recursive: true });
    fs.copyFileSync(srcPath, dstPath);
    copied += 1;
  }

  let removed = 0;
  const dstFiles = listFilesRecursive(dstDir);
  for (const rel of dstFiles) {
    const norm = rel.replace(/\\/g, '/');
    if (excludeRelPrefixes.some((p) => norm.startsWith(p))) continue;
    if (preserveFilter && preserveFilter(norm)) continue;
    if (!srcSet.has(norm)) {
      fs.rmSync(path.join(dstDir, rel), { force: true });
      removed += 1;
    }
  }
  return { copied, removed };
}

function getMasterShortSha() {
  try {
    return run('git', ['rev-parse', '--short', 'HEAD'], { capture: true }).trim();
  } catch {
    return 'local';
  }
}

function syncPush() {
  const tmpBase = fs.mkdtempSync(path.join(os.tmpdir(), 'syrinx-aistudio-push-'));
  const exportDir = path.join(tmpBase, 'export');
  const cloneDir = path.join(tmpBase, 'repo');

  try {
    // 1. Export fresh standalone AI Studio bundle
    run(process.execPath, [path.join(repoRoot, 'scripts', 'export-ai-studio.mjs'), '--out', exportDir]);

    // 2. Check if remote branch exists (preserves commit history for clean 2-way sync!)
    let remoteHasBranch = false;
    try {
      const lsOut = run('git', ['ls-remote', '--heads', remoteRepo, branch], { capture: true });
      remoteHasBranch = lsOut.trim().length > 0;
    } catch {
      remoteHasBranch = false;
    }

    fs.mkdirSync(cloneDir, { recursive: true });
    if (remoteHasBranch) {
      run('git', ['clone', '--branch', branch, '--single-branch', remoteRepo, cloneDir]);
    } else {
      run('git', ['init', '-b', branch], { cwd: cloneDir });
      run('git', ['remote', 'add', 'origin', remoteRepo], { cwd: cloneDir });
    }

    // 3. Mirror exported bundle into cloneDir while preserving .git/ and AI Studio runtime files
    const preservedTopLevel = new Set([
      '.git',
      'server.ts',
      'bun.lock',
      '.env.example',
      'metadata.json',
      'package.json',
    ]);
    const hadRemotePkg = fs.existsSync(path.join(cloneDir, 'package.json'));

    if (remoteHasBranch) {
      // Remove stale files in src/ and mirror src/ cleanly from exportDir
      copyDirClean(path.join(exportDir, 'src'), path.join(cloneDir, 'src'));

      // Copy non-preserved top-level files from exportDir (index.html, vite.config.ts, tsconfig.json, .env, etc.)
      for (const entry of fs.readdirSync(exportDir, { withFileTypes: true })) {
        if (entry.name === 'src' || preservedTopLevel.has(entry.name)) continue;
        const srcPath = path.join(exportDir, entry.name);
        const dstPath = path.join(cloneDir, entry.name);
        if (entry.isFile()) {
          fs.copyFileSync(srcPath, dstPath);
        }
      }

      // Merge any newly added web dependencies into AI Studio's existing package.json
      if (hadRemotePkg) {
        const remotePkgPath = path.join(cloneDir, 'package.json');
        const exportPkg = JSON.parse(fs.readFileSync(path.join(exportDir, 'package.json'), 'utf8'));
        const remotePkg = JSON.parse(fs.readFileSync(remotePkgPath, 'utf8'));
        remotePkg.dependencies = {
          ...(exportPkg.dependencies ?? {}),
          ...(remotePkg.dependencies ?? {}),
        };
        remotePkg.devDependencies = {
          ...(exportPkg.devDependencies ?? {}),
          ...(remotePkg.devDependencies ?? {}),
        };
        fs.writeFileSync(remotePkgPath, JSON.stringify(remotePkg, null, 2) + '\n', 'utf8');
      } else {
        fs.copyFileSync(path.join(exportDir, 'package.json'), path.join(cloneDir, 'package.json'));
      }
    } else {
      for (const entry of fs.readdirSync(cloneDir)) {
        if (entry === '.git') continue;
        fs.rmSync(path.join(cloneDir, entry), { recursive: true, force: true });
      }
      copyDirClean(exportDir, cloneDir);
    }

    // 4. Commit and push if anything changed
    run('git', ['add', '-A'], { cwd: cloneDir });
    const status = run('git', ['status', '--porcelain'], { cwd: cloneDir, capture: true }).trim();
    if (!status) {
      console.log(`Already up to date with ${remoteRepo} (${branch}).`);
      return;
    }

    const sha = getMasterShortSha();
    const msg = customMessage || `Sync SyrinxOne Web UI from monorepo (${sha})`;
    run('git', ['commit', '-m', msg], { cwd: cloneDir });
    run('git', ['push', '-u', 'origin', branch], { cwd: cloneDir });
    console.log(`\nPushed standalone Web UI bundle to ${remoteRepo} (${branch}).`);
  } finally {
    fs.rmSync(tmpBase, { recursive: true, force: true });
  }
}

function syncPull() {
  const tmpBase = fs.mkdtempSync(path.join(os.tmpdir(), 'syrinx-aistudio-pull-'));
  const cloneDir = path.join(tmpBase, 'repo');

  try {
    run('git', ['clone', '--branch', branch, '--single-branch', '--depth', '1', remoteRepo, cloneDir]);

    const webDir = path.join(repoRoot, 'apps', 'web');
    const pulledIndex = path.join(cloneDir, 'index.html');
    if (fs.existsSync(pulledIndex)) {
      fs.copyFileSync(pulledIndex, path.join(webDir, 'index.html'));
    }
    const pulledFirebaseCfg = path.join(cloneDir, 'firebase-applet-config.json');
    if (fs.existsSync(pulledFirebaseCfg)) {
      fs.copyFileSync(pulledFirebaseCfg, path.join(webDir, 'firebase-applet-config.json'));
    }

    const pulledSrc = path.join(cloneDir, 'src');
    if (!fs.existsSync(pulledSrc)) {
      throw new Error(`Expected src/ directory in ${remoteRepo} (${branch})`);
    }

    const { copied, removed } = copyDirClean(pulledSrc, path.join(webDir, 'src'), {
      excludeRelPrefixes: ['shared/'],
      // Preserve monorepo unit tests that are omitted from the AI Studio export
      preserveFilter: (norm) => norm.endsWith('.test.ts') || norm.endsWith('.test.tsx'),
    });

    console.log(`\nSynced ${copied} file(s) from ${remoteRepo} (${branch}) into apps/web/src/ (${removed} removed).`);
    console.log('Review changes with: git status && git diff apps/web');
    console.log('Then verify with:    npm.cmd run build && npm.cmd run typecheck && npm.cmd run test:js');
  } finally {
    fs.rmSync(tmpBase, { recursive: true, force: true });
  }
}

if (mode === 'push') {
  syncPush();
} else if (mode === 'pull') {
  syncPull();
} else {
  console.error('Usage: node scripts/sync-ai-studio.mjs <push|pull> [--repo URL] [--branch main]');
  process.exit(1);
}
