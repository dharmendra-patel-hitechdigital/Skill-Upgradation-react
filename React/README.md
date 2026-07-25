# Hitech · React Login + Dashboard

A production-style React app demonstrating **components**, **custom hooks**, and **API integration**, with a polished, responsive UI. It runs end-to-end with **zero backend** thanks to a built-in mock API — swap in a real server by setting one env var.

## Features

- 🔐 **Login** — form validation, show/hide password, error handling, demo credentials.
- 📊 **Dashboard** — stat cards, a dependency-free bar chart, and a recent-activity feed.
- 🧭 **Routing** — protected routes, session restore, redirect-after-login (React Router v6).
- 🪝 **Custom hooks** — `useAuth`, `useApi` (loading/error/abort), `useDocumentTitle`.
- 🌐 **API layer** — central `fetch` client with bearer-token injection and normalized errors.
- 📱 **Responsive** — collapsible sidebar, fluid grids, mobile-friendly down to 360px.

## Quick start

```bash
npm install
npm run dev
```

Open http://localhost:5173 and sign in with:

| Email               | Password       |
| ------------------- | -------------- |
| `demo@hitech.com`   | `password123`  |

## Connecting a real backend

The app uses an in-memory mock API by default. To call a real server, create a
`.env` file (see `.env.example`):

```
VITE_API_BASE_URL=https://api.your-domain.com
```

Expected endpoints:

| Method | Path                  | Returns                |
| ------ | --------------------- | ---------------------- |
| POST   | `/auth/login`         | `{ token, user }`      |
| GET    | `/auth/me`            | `{ user }`             |
| GET    | `/dashboard/stats`    | `{ stats: [...] }`     |
| GET    | `/dashboard/revenue`  | `{ series: [...] }`    |
| GET    | `/dashboard/activity` | `{ activity: [...] }`  |

Once `VITE_API_BASE_URL` is set, the mock server (`src/api/mockServer.js`) is
bypassed and can be deleted.

## Project structure

```
src/
├── api/            # fetch client + endpoint modules + mock backend
├── components/
│   ├── ui/         # Button, Input, Card, Spinner
│   ├── layout/     # Sidebar, Topbar, ProtectedRoute
│   └── dashboard/  # StatCard, RevenueChart, RecentActivity
├── context/        # AuthProvider
├── hooks/          # useAuth, useApi, useDocumentTitle
├── pages/          # Login, Dashboard, NotFound
└── styles/         # global.css (design tokens + components)
```

## Scripts

| Command           | Description                  |
| ----------------- | ---------------------------- |
| `npm run dev`     | Start the dev server         |
| `npm run build`   | Production build to `dist/`  |
| `npm run preview` | Preview the production build |
