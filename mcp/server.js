import { spawn, execSync } from 'child_process';
import express from 'express';
import cors from 'cors';
import bodyParser from 'body-parser';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

let isShuttingDown = false;

function shutdown(signal) {
  if (isShuttingDown) return;
  isShuttingDown = true;
  
  console.log(`Received ${signal}, shutting down MCP servers...`);
  
  for (const [name, server] of Object.entries(servers)) {
    if (server.process && !server.process.killed) {
      console.log(`Killing ${name}...`);
      server.process.kill('SIGTERM');
    }
  }
  
  setTimeout(() => {
    for (const [name, server] of Object.entries(servers)) {
      if (server.process && !server.process.killed) {
        console.log(`Force killing ${name}...`);
        server.process.kill('SIGKILL');
      }
    }
    process.exit(0);
  }, 5000);
}

process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('SIGINT', () => shutdown('SIGINT'));

const app = express();
app.use(cors());
app.use(bodyParser.json());

const servers = {};

function applyEnvOverrides(config) {
  const envPrefix = `MCP_${config.name.toUpperCase()}_`;
  
  const commandEnvKey = `${envPrefix}COMMAND`;
  if (process.env[commandEnvKey]) {
    config.command = process.env[commandEnvKey];
  }
  
  const argsEnvKey = `${envPrefix}ARGS`;
  if (process.env[argsEnvKey]) {
    try {
      config.args = JSON.parse(process.env[argsEnvKey]);
    } catch (e) {
      console.error(`Failed to parse ${argsEnvKey}: ${e.message}`);
    }
  }

  const cliArgKeys = Object.keys(process.env).filter(k => k.startsWith(envPrefix) && !['COMMAND', 'ARGS'].includes(k.slice(envPrefix.length)));
  for (const key of cliArgKeys) {
    const argName = key.slice(envPrefix.length).replace(/_/g, '-').toLowerCase();
    const value = process.env[key];
    if (value) {
      config.args.push(`--${argName}`, value);
    }
  }
  
  for (const [key, value] of Object.entries(process.env)) {
    if (key.startsWith(envPrefix) && key !== commandEnvKey && key !== argsEnvKey && !cliArgKeys.includes(key)) {
      const envVarName = key.slice(envPrefix.length);
      config.env[envVarName] = value;
    }
  }
  
  return config;
}

function extractPackageName(command, args) {
  if (!args || args.length === 0) return null;

  if (command === 'npx' || command === 'npm' || command === 'uvx') {
    return null;
  }

  if (command === 'node') {
    const nodeModulePath = args.find(arg => arg.includes('/node_modules/'));
    if (nodeModulePath) {
      const match = nodeModulePath.match(/node_modules\/(@[^/]+\/[^/]+)/);
      if (match) return match[1];
    }
  }

  return null;
}

async function syncDependencies() {
  const configPath = path.join(__dirname, 'mcp-servers.json');
  const packageJsonPath = path.join(__dirname, 'package.json');

  let serverConfigs;
  try {
    const configData = fs.readFileSync(configPath, 'utf8');
    serverConfigs = JSON.parse(configData).servers;
  } catch (err) {
    console.log(`No mcp-servers.json found or error reading it: ${err.message}`);
    return;
  }

  const requiredPackages = new Set();
  for (const config of serverConfigs) {
    const packageName = extractPackageName(config.command, config.args);
    if (packageName) {
      requiredPackages.add(packageName);
    }
  }

  let packageJson;
  try {
    const packageData = fs.readFileSync(packageJsonPath, 'utf8');
    packageJson = JSON.parse(packageData);
  } catch (err) {
    console.error(`Failed to read package.json: ${err.message}`);
    return;
  }

  const currentDeps = new Set([
    ...Object.keys(packageJson.dependencies || {}),
    ...Object.keys(packageJson.devDependencies || {})
  ]);

  const missingPackages = [...requiredPackages].filter(p => !currentDeps.has(p));

  const protectedPackages = new Set([
    'express', 'cors', 'body-parser'
  ]);

  const packagesToRemove = [...currentDeps].filter(p =>
    !requiredPackages.has(p) && !protectedPackages.has(p)
  );

  let needsNpmInstall = false;

  if (missingPackages.length > 0) {
    console.log(`Adding missing dependencies: ${missingPackages.join(', ')}`);

    for (const pkg of missingPackages) {
      packageJson.dependencies[pkg] = 'latest';
    }
    needsNpmInstall = true;
  }

  if (packagesToRemove.length > 0) {
    console.log(`Removing unused dependencies: ${packagesToRemove.join(', ')}`);

    for (const pkg of packagesToRemove) {
      delete packageJson.dependencies[pkg];
    }
    needsNpmInstall = true;
  }

  if (missingPackages.length === 0 && packagesToRemove.length === 0) {
    console.log('All dependencies already in package.json');
    return;
  }

  fs.writeFileSync(packageJsonPath, JSON.stringify(packageJson, null, 2) + '\n');

  if (needsNpmInstall) {
    console.log('Running npm install...');
    try {
      execSync('npm install', { cwd: __dirname, stdio: 'inherit' });
      console.log('Dependencies installed successfully');
    } catch (err) {
      console.error(`npm install failed: ${err.message}`);
    }
  }
}

function createMCPServer(name, command, args, env = {}, cwd = '/app') {
  return new Promise((resolve, reject) => {
    const serverProcess = spawn(command, args, {
      env: { ...process.env, ...env },
      stdio: ['pipe', 'pipe', 'pipe'],
      cwd
    });

    let initialized = false;
    let tools = [];
    let requestId = 0;
    const pendingRequests = {};

    serverProcess.stderr.on('data', (data) => {
      console.error(`[${name}] stderr: ${data}`);
    });

    serverProcess.on('error', (err) => {
      console.error(`[${name}] error: ${err}`);
    });

    serverProcess.on('close', (code) => {
      console.log(`[${name}] process exited with code ${code}`);
      if (!initialized) {
        reject(new Error(`Process exited with code ${code} before initialization`));
      }
    });

    function sendRequest(method, params = {}) {
      return new Promise((resolve, reject) => {
        const id = ++requestId;
        const request = JSON.stringify({
          jsonrpc: '2.0',
          id,
          method,
          params
        });

        pendingRequests[id] = { resolve, reject };

        serverProcess.stdin.write(request + '\n');
      });
    }

    serverProcess.stdout.on('data', (data) => {
      const lines = data.toString().split('\n').filter(line => line.trim());
      
      for (const line of lines) {
        try {
          const response = JSON.parse(line);
          
          if (response.id && pendingRequests[response.id]) {
            const { resolve, reject } = pendingRequests[response.id];
            delete pendingRequests[response.id];
            
            if (response.error) {
              reject(new Error(response.error.message));
            } else {
              resolve(response.result);
            }
          } else if (response.method === 'notifications/tools/changed') {
            // Tools changed notification - refresh tools
            console.log(`[${name}] tools changed, refreshing...`);
          }
        } catch (e) {
          console.error(`[${name}] failed to parse response: ${e.message}`);
        }
      }
    });

    // Initialize the MCP server
    sendRequest('initialize', {
      protocolVersion: '2024-11-05',
      capabilities: {},
      clientInfo: {
        name: 'unmute-mcp-bridge',
        version: '1.0.0'
      }
    }).then(() => {
      initialized = true;
      console.log(`[${name}] initialized`);
      
      // Get tools
      return sendRequest('tools/list');
    }).then((result) => {
      tools = result.tools || [];
      console.log(`[${name}] loaded ${tools.length} tools`);
      
      servers[name] = {
        process: serverProcess,
        tools,
        sendRequest,
        callTool: (toolName, args) => sendRequest('tools/call', {
          name: toolName,
          arguments: args
        })
      };
      
      resolve();
    }).catch((err) => {
      console.error(`[${name}] initialization failed: ${err.message}`);
      reject(err);
    });
  });
}

async function initializeServers() {
  const configPath = path.join(__dirname, 'mcp-servers.json');
  let serverConfigs;
  
  try {
    const configData = fs.readFileSync(configPath, 'utf8');
    serverConfigs = JSON.parse(configData).servers;
  } catch (err) {
    console.error(`Failed to load mcp-servers.json: ${err.message}`);
    process.exit(1);
  }
  
  serverConfigs = serverConfigs.map(config => applyEnvOverrides(config));

  console.log('Initializing MCP servers...');
  
  for (const config of serverConfigs) {
    console.log(`Starting ${config.name}...`);
    try {
      await createMCPServer(config.name, config.command, config.args, config.env, config.cwd);
      console.log(`Started ${config.name} successfully`);
    } catch (err) {
      console.error(`Failed to start ${config.name}: ${err.message}`);
    }
  }
  
  const activeServers = Object.keys(servers).length;
  console.log(`MCP servers initialized: ${activeServers} active`);
}

app.get('/health', (req, res) => {
  res.json({ status: 'ok', servers: Object.keys(servers) });
});

app.get('/v1/mcp/tools', (req, res) => {
  const allTools = [];
  
  for (const [serverName, server] of Object.entries(servers)) {
    for (const tool of server.tools) {
      allTools.push({
        name: tool.name,
        description: tool.description,
        inputSchema: tool.inputSchema,
        server_name: serverName
      });
    }
  }
  
  res.json(allTools);
});

app.post('/v1/mcp/call', async (req, res) => {
  const { tool_name, arguments: args } = req.body;
  
  if (!tool_name) {
    return res.status(400).json({ error: 'tool_name is required' });
  }

  for (const [serverName, server] of Object.entries(servers)) {
    const tool = server.tools.find(t => t.name === tool_name);
    
    if (tool) {
      try {
        const result = await server.callTool(tool_name, args || {});
        res.json(result);
        return;
      } catch (err) {
        res.status(500).json({ error: err.message });
        return;
      }
    }
  }
  
  res.status(404).json({ error: `Tool '${tool_name}' not found` });
});

app.get('/v1/mcp/status', (req, res) => {
  const status = {};
  
  for (const [serverName, server] of Object.entries(servers)) {
    status[serverName] = {
      connected: server.process && !server.process.killed,
      tools_count: server.tools.length
    };
  }
  
  res.json(status);
});

const PORT = process.env.MCP_PORT || 3001;

syncDependencies()
  .then(() => initializeServers())
  .then(() => {
    console.log('MCP servers initialized, starting HTTP server...');
    app.listen(PORT, '127.0.0.1', () => {
      console.log(`MCP bridge listening on http://127.0.0.1:${PORT}`);
    });
  })
  .catch((err) => {
    console.error('Failed to initialize servers:', err);
    process.exit(1);
  });
