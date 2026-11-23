import { spawn, type ChildProcess } from 'node:child_process';
import crypto from 'node:crypto';
import fs from 'node:fs';
import net from 'node:net';
import path from 'node:path';

export interface SidecarHandle {
  baseUrl: string;
  token: string;
  stop: () => void;
}

function findFreePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.unref();
    srv.on('error', reject);
    srv.listen(0, '127.0.0.1', () => {
      const addr = srv.address();
      if (addr && typeof addr === 'object') {
        const port = addr.port;
        srv.close(() => resolve(port));
      } else {
        srv.close(() => reject(new Error('Failed to bind ephemeral localhost port')));
      }
    });
  });
}

async function waitForHealth(baseUrl: string, timeoutMs = 20000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(`${baseUrl}/api/health`);
      if (res.ok) return;
    } catch {
      // backend still booting
    }
    await new Promise((r) => setTimeout(r, 250));
  }
  throw new Error(`Python sidecar did not become healthy at ${baseUrl} within ${timeoutMs}ms`);
}

export async function startPythonSidecar(repoRoot: string): Promise<SidecarHandle> {
  const port = await findFreePort();
  const token = crypto.randomBytes(24).toString('hex');
  const baseUrl = `http://127.0.0.1:${port}`;
  const backendDir = path.join(repoRoot, 'backend', 'python');

  const venvPython =
    process.platform === 'win32'
      ? path.join(backendDir, '.venv', 'Scripts', 'python.exe')
      : path.join(backendDir, '.venv', 'bin', 'python');

  const env = {
    ...process.env,
    SYRINX_API_TOKEN: token,
    PYTHONUNBUFFERED: '1',
  };

  let child: ChildProcess;
  if (fs.existsSync(venvPython)) {
    child = spawn(
      venvPython,
      ['-m', 'uvicorn', 'syrinx_backend.api.app:app', '--host', '127.0.0.1', '--port', String(port)],
      { cwd: backendDir, env, stdio: 'pipe', windowsHide: true },
    );
  } else {
    child = spawn(
      'uv',
      [
        'run',
        'uvicorn',
        'syrinx_backend.api.app:app',
        '--host',
        '127.0.0.1',
        '--port',
        String(port),
      ],
      { cwd: backendDir, env, stdio: 'pipe', shell: process.platform === 'win32', windowsHide: true },
    );
  }

  child.stdout?.on('data', (d) => process.stdout.write(`[python] ${String(d)}`));
  child.stderr?.on('data', (d) => process.stderr.write(`[python] ${String(d)}`));

  await waitForHealth(baseUrl);

  const stop = () => {
    if (!child.pid || child.killed) return;
    if (process.platform === 'win32') {
      spawn('taskkill', ['/pid', String(child.pid), '/T', '/F'], { stdio: 'ignore' });
    } else {
      child.kill('SIGTERM');
    }
  };

  return { baseUrl, token, stop };
}
