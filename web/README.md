# Web interface

React, TypeScript, TanStack Query, and Three.js client for the 3D Printing Agent.

```powershell
npm.cmd install
npm.cmd run dev
npm.cmd run lint
npm.cmd run build
```

The development server proxies `/api` to `http://127.0.0.1:8000`. A production build is served automatically by FastAPI when `web/dist` exists.
