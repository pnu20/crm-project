const path = require('node:path');
require('dotenv').config({ path: path.resolve(__dirname, '..', '.env') });

const express = require('express');
const cors = require('cors');
const { Pool } = require('pg');

const app = express();
const port = Number(process.env.PORT || 3000);
const requiredDatabaseSettings = ['PGHOST', 'PGPORT', 'PGDATABASE', 'PGUSER', 'PGPASSWORD'];
const missingDatabaseSettings = requiredDatabaseSettings.filter(name => !process.env[name]);

if (missingDatabaseSettings.length > 0) {
  throw new Error(`Missing PostgreSQL settings: ${missingDatabaseSettings.join(', ')}`);
}

const databasePort = Number(process.env.PGPORT);
if (!Number.isInteger(databasePort) || databasePort < 1 || databasePort > 65535) {
  throw new Error('PGPORT must be a valid TCP port number');
}

if (!Number.isInteger(port) || port < 1 || port > 65535) {
  throw new Error('PORT must be a valid TCP port number');
}

const pool = new Pool({
  host: process.env.PGHOST,
  port: databasePort,
  database: process.env.PGDATABASE,
  user: process.env.PGUSER,
  password: process.env.PGPASSWORD,
});

app.use(express.json({ limit: '1mb' }));
app.use(cors({
  origin: [
    'http://127.0.0.1:8000',
    'http://localhost:8000',
    'http://127.0.0.1:5500',
    'http://localhost:5500',
  ],
}));

async function initializeDatabase() {
  await pool.query(`
    CREATE TABLE IF NOT EXISTS customers (
      id SERIAL PRIMARY KEY,
      name VARCHAR(100) NOT NULL,
      email VARCHAR(100),
      phone VARCHAR(20),
      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
  `);
}

app.get('/', async (_request, response) => {
  try {
    const result = await pool.query('SELECT NOW() AS server_time');
    response.json({
      status: 'ok',
      message: 'PostgreSQL connection successful',
      database: process.env.PGDATABASE || 'postgres',
      serverTime: result.rows[0].server_time,
    });
  } catch (error) {
    console.error('PostgreSQL connection test failed:', error.message);
    response.status(503).json({
      status: 'error',
      message: 'PostgreSQL connection failed',
    });
  }
});

app.get('/api/health', (_request, response) => {
  response.json({ status: 'ok', service: 'crm-node-api' });
});

app.post('/api/customers', async (request, response, next) => {
  try {
    const { name, email = null, phone = null } = request.body || {};

    if (typeof name !== 'string' || name.trim().length === 0 || name.trim().length > 100) {
      return response.status(400).json({
        status: 'error',
        message: 'Customer name is required and must be 100 characters or fewer',
      });
    }
    if (email !== null && (typeof email !== 'string' || email.length > 100)) {
      return response.status(400).json({ status: 'error', message: 'Email must be 100 characters or fewer' });
    }
    if (phone !== null && (typeof phone !== 'string' || phone.length > 20)) {
      return response.status(400).json({ status: 'error', message: 'Phone must be 20 characters or fewer' });
    }

    const result = await pool.query(
      `INSERT INTO customers (name, email, phone)
       VALUES ($1, $2, $3)
       RETURNING id, name, email, phone, created_at`,
      [name.trim(), email, phone],
    );

    return response.status(201).json({
      status: 'ok',
      customer: result.rows[0],
    });
  } catch (error) {
    return next(error);
  }
});

app.get('/api/customers', async (_request, response, next) => {
  try {
    const result = await pool.query(
      `SELECT id, name, email, phone, created_at
       FROM customers
       ORDER BY created_at DESC, id DESC`,
    );

    return response.json({
      status: 'ok',
      customers: result.rows,
    });
  } catch (error) {
    return next(error);
  }
});
app.use((_request, response) => {
  response.status(404).json({ status: 'error', message: 'Not found' });
});

app.use((error, _request, response, _next) => {
  console.error('Unhandled error:', error);
  response.status(500).json({ status: 'error', message: 'Internal server error' });
});

pool.on('error', (error) => {
  console.error('Unexpected PostgreSQL pool error:', error.message);
});

let server;

async function start() {
  await initializeDatabase();
  server = app.listen(port);
  await new Promise((resolve, reject) => {
    server.once('listening', resolve);
    server.once('error', reject);
  });
  console.log(`CRM Node API listening on http://localhost:${port}`);
  return server;
}

async function shutdown(signal) {
  console.log(`${signal} received; closing Node API.`);
  if (server?.listening) {
    await new Promise((resolve) => server.close(resolve));
  }
  await pool.end();
}

if (require.main === module) {
  start().catch(async (error) => {
    console.error('Failed to start CRM Node API:', error.message);
    await pool.end();
    process.exitCode = 1;
  });

  process.once('SIGINT', () => shutdown('SIGINT'));
  process.once('SIGTERM', () => shutdown('SIGTERM'));
}

module.exports = { app, start, pool, initializeDatabase };
