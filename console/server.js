// OpsGentic Console — a small BFF: proxies the opsgentic API (chat/runs/approve) and edits the
// opsgentic ConfigMaps/Secret live via the Kubernetes API (with a namespaced ServiceAccount).
'use strict';
const express = require('express');
const fs = require('fs');
const https = require('https');

const PORT = process.env.PORT || 3000;
const NS = process.env.OPSGENTIC_NAMESPACE || 'opsgentic';
const API = (process.env.OPSGENTIC_API_URL || 'http://opsgentic.opsgentic.svc:80').replace(/\/$/, '');
const CONFIG_CM = 'opsgentic-config';
const SKILLS_CM = 'opsgentic-skills';
const SECRET = 'opsgentic-secrets';
const DEPLOYMENTS = (process.env.OPSGENTIC_DEPLOYMENTS || 'opsgentic,opsgentic-worker').split(',');

// --- Kubernetes in-cluster API (via the mounted ServiceAccount) ---------------------------
const SA = '/var/run/secrets/kubernetes.io/serviceaccount';
const readToken = () => fs.readFileSync(`${SA}/token`, 'utf8').trim();
const CA = fs.existsSync(`${SA}/ca.crt`) ? fs.readFileSync(`${SA}/ca.crt`) : undefined;

function k8s(method, path, body, contentType) {
  return new Promise((resolve, reject) => {
    const data = body ? JSON.stringify(body) : null;
    const headers = { Authorization: `Bearer ${readToken()}`, Accept: 'application/json' };
    if (data) { headers['Content-Type'] = contentType || 'application/json'; headers['Content-Length'] = Buffer.byteLength(data); }
    const req = https.request({ host: 'kubernetes.default.svc', path, method, ca: CA, headers }, (r) => {
      let buf = '';
      r.on('data', (d) => (buf += d));
      r.on('end', () => {
        let parsed; try { parsed = buf ? JSON.parse(buf) : {}; } catch { parsed = buf; }
        if (r.statusCode >= 200 && r.statusCode < 300) resolve(parsed);
        else reject(Object.assign(new Error(`k8s ${r.statusCode}`), { status: r.statusCode, body: parsed }));
      });
    });
    req.on('error', reject);
    if (data) req.write(data);
    req.end();
  });
}
const cmPath = (n) => `/api/v1/namespaces/${NS}/configmaps/${n}`;
const secretPath = (n) => `/api/v1/namespaces/${NS}/secrets/${n}`;
const MERGE = 'application/merge-patch+json';

async function restartDeployments() {
  const ts = new Date().toISOString();
  const patch = { spec: { template: { metadata: { annotations: { 'opsgentic.io/restartedAt': ts } } } } };
  for (const d of DEPLOYMENTS) {
    try { await k8s('PATCH', `/apis/apps/v1/namespaces/${NS}/deployments/${d}`, patch, MERGE); } catch (e) { /* best-effort */ }
  }
}

// --- opsgentic API proxy -------------------------------------------------------------------
async function proxy(res, method, path, body) {
  try {
    const r = await fetch(`${API}${path}`, {
      method, headers: { 'content-type': 'application/json' }, body: body ? JSON.stringify(body) : undefined,
    });
    res.status(r.status).type('application/json').send(await r.text());
  } catch (e) {
    res.status(502).json({ error: `opsgentic API unreachable: ${e.message}` });
  }
}

const app = express();
app.use(express.json({ limit: '1mb' }));
app.use(express.static(__dirname + '/public'));
app.get('/healthz', (_req, res) => res.json({ status: 'ok' }));

// runs / chat / approval -> opsgentic
app.post('/api/chat', (req, res) => proxy(res, 'POST', '/chat', req.body));
app.get('/api/runs', (_req, res) => proxy(res, 'GET', '/runs'));
app.get('/api/runs/:id', (req, res) => proxy(res, 'GET', `/runs/${encodeURIComponent(req.params.id)}`));
app.post('/api/runs/:id/approve', (req, res) => proxy(res, 'POST', `/runs/${encodeURIComponent(req.params.id)}/approve`));
app.post('/api/runs/:id/reject', (req, res) => proxy(res, 'POST', `/runs/${encodeURIComponent(req.params.id)}/reject`));

// graph visualize (system topology + per-run trace) -> opsgentic
app.get('/api/graph', (_req, res) => proxy(res, 'GET', '/graph'));
app.get('/api/graph/tools/:server', (req, res) => proxy(res, 'GET', `/graph/tools/${encodeURIComponent(req.params.server)}`));
app.get('/api/runs/:id/graph', (req, res) => proxy(res, 'GET', `/runs/${encodeURIComponent(req.params.id)}/graph`));

// skills (opsgentic-skills ConfigMap) ------------------------------------------------------
app.get('/api/skills', async (_req, res) => {
  try {
    const cm = await k8s('GET', cmPath(SKILLS_CM));
    res.json({ skills: cm.data || {} });
  } catch (e) { res.status(e.status || 500).json({ error: String(e.message || e) }); }
});
app.put('/api/skills/:name', async (req, res) => {
  try {
    const name = req.params.name;
    await k8s('PATCH', cmPath(SKILLS_CM), { data: { [name]: String(req.body.content || '') } }, MERGE);
    await restartDeployments();
    res.json({ ok: true, restarted: true });
  } catch (e) { res.status(e.status || 500).json({ error: String(e.message || e) }); }
});

// agent + MCP config (opsgentic-config ConfigMap) and key presence (opsgentic-secrets) -------
app.get('/api/config', async (_req, res) => {
  try {
    const cm = await k8s('GET', cmPath(CONFIG_CM));
    let secretKeys = {};
    try { const s = await k8s('GET', secretPath(SECRET)); secretKeys = Object.keys(s.data || {}).reduce((a, k) => (a[k] = true, a), {}); } catch { /* may not exist */ }
    res.json({ config: cm.data || {}, secrets: secretKeys });
  } catch (e) { res.status(e.status || 500).json({ error: String(e.message || e) }); }
});
app.put('/api/config', async (req, res) => {
  try {
    const data = req.body.data || {};
    const stringData = Object.entries(data).reduce((a, [k, v]) => (a[k] = String(v), a), {});
    await k8s('PATCH', cmPath(CONFIG_CM), { data: stringData }, MERGE);
    await restartDeployments();
    res.json({ ok: true, restarted: true });
  } catch (e) { res.status(e.status || 500).json({ error: String(e.message || e) }); }
});
// secrets: set new values (never returned). stringData is merged + base64-encoded by k8s.
app.put('/api/secrets', async (req, res) => {
  try {
    const data = req.body.data || {};
    const stringData = Object.entries(data).filter(([, v]) => v !== '' && v != null)
      .reduce((a, [k, v]) => (a[k] = String(v), a), {});
    if (Object.keys(stringData).length === 0) return res.json({ ok: true, restarted: false });
    await k8s('PATCH', secretPath(SECRET), { stringData }, MERGE);
    await restartDeployments();
    res.json({ ok: true, restarted: true });
  } catch (e) { res.status(e.status || 500).json({ error: String(e.message || e) }); }
});

app.listen(PORT, () => console.log(`opsgentic-console on :${PORT} (api=${API}, ns=${NS})`));
