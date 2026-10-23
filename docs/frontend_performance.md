# Frontend performance — diagnosis & fixes

**Symptom:** app is slow to load, shows skeleton placeholders for a long time, and
switching between tabs is slow (placeholders reappear every time).

## Root cause (measured from the code)

1. **No caching of any kind.** `lib/hooks.ts`'s `useFetch` was a bare
   `useEffect`-on-mount fetch starting at `loading=true, data=null` with no cache,
   no dedupe, no cross-route sharing. Every tab switch unmounts the page and
   remounts the next → all its `useFetch`es re-fire → skeletons reappear → wait for
   fresh HTTP round-trips, even for endpoints already fetched on another tab
   (getStats, getHeatmap, getHistorical, getModelPerformance, getAvailableModels,
   getMe are all fetched by multiple tabs). **This is the dominant cause.**
2. **Everything is client-only** (`"use client"` on every page, no `loading.tsx`,
   no `<Suspense>`) → first paint is a skeleton until the JS bundle downloads,
   hydrates, and effects run.
3. **Heavy libs imported eagerly** — `recharts` and `framer-motion` at module top in
   most pages and both chart components → large initial bundle delays hydration.
4. **Oversized / wasted requests** — Analytics fetched `getHistorical(30,"hourly")`
   (~720 pts) into a `weeklyChart` that was **never rendered**; heatmaps pull 30–60
   days (~720–1440 cells).
5. **Heatmap animated ~1440 cells individually** (`motion.div` per cell with a
   staggered delay) → main-thread jank on mount.

## What this change fixes (shipped here)

- **Shared stale-while-revalidate cache in `lib/hooks.ts`** — process-lifetime
  `Map` keyed automatically by the fetcher source + deps (so **no call-site changes
  needed**). On revisit a tab shows its last data **instantly** and revalidates
  quietly in the background; identical in-flight requests are de-duplicated and
  results are shared across tabs. This removes the repeated skeletons and makes tab
  switching feel instant. *(Fixes #1.)*
- **Heatmap de-animated** (`components/charts/heatmap-chart.tsx`) — cells are plain
  `div`s now (CSS hover kept, per-cell entrance stagger dropped), eliminating the
  ~1440-node animation jank. *(Fixes #5.)*
- **Removed the wasted Analytics fetch** (`weekly`/`weeklyChart`) — one fewer
  ~720-point request + compute on that tab. *(Fixes #4, partially.)*

## Recommended follow-ups (not in this PR — need a build/QA pass)

- **Code-split charts:** wrap `DemandChart`/`HeatmapChart` and the inline recharts
  blocks in `next/dynamic(() => import(...), { ssr:false, loading: <skeleton> })`
  so recharts leaves the initial bundle. *(Fixes #3.)*
- **Server-render / stream** the first data (RSC or route `loading.tsx` + Suspense),
  or add an aggregate endpoint (e.g. `/dashboard/overview`) returning the dashboard's
  KPIs+chart data in one response to cut 7 round-trips to 1. *(Fixes #2, #4.)*
- Trim default windows (heatmap 60→30) / downsample server-side.
- Gate the Admin page's 5s `getRetrainStatus` poll to only run while a retrain is
  actually in progress.
