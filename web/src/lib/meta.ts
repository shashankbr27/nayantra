import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { Meta } from "../api/types";

let cache: Meta | null = null;
let inflight: Promise<Meta> | null = null;

export function useMeta(): Meta | null {
  const [meta, setMeta] = useState<Meta | null>(cache);
  useEffect(() => {
    if (cache) return;
    inflight ??= api.get<Meta>("/meta");
    inflight.then((m) => {
      cache = m;
      setMeta(m);
    }).catch(() => {
      inflight = null;
    });
  }, []);
  return meta;
}
