import express, { type Express } from "express";
import cors from "cors";
import pinoHttp from "pino-http";
import { createProxyMiddleware, fixRequestBody } from "http-proxy-middleware";
import router from "./routes";
import { logger } from "./lib/logger";

const app: Express = express();

app.use(
  pinoHttp({
    logger,
    serializers: {
      req(req) {
        return {
          id: req.id,
          method: req.method,
          url: req.url?.split("?")[0],
        };
      },
      res(res) {
        return {
          statusCode: res.statusCode,
        };
      },
    },
  }),
);
app.use(cors());
app.use(express.json());
app.use(express.urlencoded({ extended: true }));

// Local routes (health check served by this Node.js server)
app.use("/api", router);

// Proxy everything else under /api to the Python FastAPI backend
const pythonApiUrl = process.env.PYTHON_API_URL ?? "http://localhost:8000";

logger.info({ pythonApiUrl }, "Proxying /api to Python backend");

// Python FastAPI uses /api/v1/* prefix, but our frontend calls /api/*
// Proxy rewrites: /api/configs → /api/v1/configs, etc.
app.use(
  "/api",
  createProxyMiddleware({
    target: pythonApiUrl,
    changeOrigin: true,
    pathRewrite: { "^/": "/api/v1/" },
    on: {
      // Re-stream the parsed JSON body that express.json() already consumed,
      // otherwise POST/PUT/PATCH requests hang on the upstream.
      proxyReq: fixRequestBody,
      error(err, _req, res) {
        logger.error({ err }, "Proxy error — is the Python backend running?");
        if (res && "status" in res && typeof res.status === "function") {
          res.status(502).json({
            error: "Python backend unavailable",
            detail: `Could not reach ${pythonApiUrl}. Make sure the Python FastAPI server is running.`,
            hint: "Set PYTHON_API_URL env var if it runs on a different port (default: 8000).",
          });
        }
      },
    },
  }),
);

export default app;
