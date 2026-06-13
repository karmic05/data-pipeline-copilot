import type { NextConfig } from "next";
import path from "node:path";

const nextConfig: NextConfig = {
  // Pin the workspace root to this project so Next.js does not pick up an
  // unrelated parent lockfile (e.g. C:\Users\jaina\package-lock.json).
  turbopack: {
    root: path.resolve(__dirname),
  },
};

export default nextConfig;
