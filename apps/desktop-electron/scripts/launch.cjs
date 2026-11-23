const { spawn } = require('node:child_process');
const electronPath = require('electron');

const env = { ...process.env };
delete env.ELECTRON_RUN_AS_NODE;

const child = spawn(electronPath, ['.'], {
  cwd: require('node:path').resolve(__dirname, '..'),
  env,
  stdio: 'inherit',
});

child.on('exit', (code) => process.exit(code ?? 0));
