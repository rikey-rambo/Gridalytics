"use client";
import { useState, useEffect, useCallback, useRef } from "react";

/**
 * Process-lifetime caches shared across every component and route.
 *
 * This is what makes tab switching fast: without it, each navigation unmounts
 * the page and remounts the next, so every `useFetch` resets to
 * `loading=true, data=null` and re-hits the network — the skeletons reappear
 * every single time. With a shared cache we show the last value instantly and
 * revalidate quietly in the background (stale-while-revalidate), and identical
 * in-flight requests are de-duplicated.
 */
const cache = new Map<string, unknown>();
const inflight = new Map<string, Promise<unknown>>();

export interface FetchOptions {
  /**
   * Explicit cache key. Defaults to the fetcher's source + deps, which is
   * stable per call-site and shared across tabs that hit the same endpoint.
   * Pass an explicit key if the fetcher closes over values that vary.
   */
  key?: string;
  /** Set to false to bypass the cache for this call. */
  cache?: boolean;
}

export function useFetch<T>(
  fetcher: () => Promise<T>,
  deps: any[] = [],
  options: FetchOptions = {},
) {
  const useCache = options.cache !== false;
  const key = useCache
    ? options.key ?? `${fetcher.toString()}|${JSON.stringify(deps)}`
    : null;

  const hasInitial = key != null && cache.has(key);
  const [data, setData] = useState<T | null>(hasInitial ? (cache.get(key!) as T) : null);
  const [loading, setLoading] = useState<boolean>(!hasInitial);
  const [error, setError] = useState<string | null>(null);
  const mounted = useRef(true);

  const refetch = useCallback((silent?: unknown) => {
    const quiet = silent === true;
    setError(null);
    const cached = key != null && cache.has(key);
    if (!cached && !quiet) setLoading(true);

    let p: Promise<T>;
    if (key != null && inflight.has(key)) {
      p = inflight.get(key) as Promise<T>;
    } else {
      p = fetcher();
      if (key != null) inflight.set(key, p as Promise<unknown>);
    }

    return p
      .then((d) => {
        if (key != null) cache.set(key, d);
        if (mounted.current) setData(d);
      })
      .catch((e: any) => {
        if (mounted.current) setError(e?.message ?? String(e));
      })
      .finally(() => {
        if (key != null) inflight.delete(key);
        if (mounted.current) setLoading(false);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  useEffect(() => {
    mounted.current = true;
    if (key != null && cache.has(key)) {
      // Show cached value immediately, then revalidate without flashing a skeleton.
      setData(cache.get(key) as T);
      setLoading(false);
      refetch(true);
    } else {
      refetch(false);
    }
    return () => {
      mounted.current = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refetch]);

  return { data, loading, error, refetch };
}
