// Server-side proxy to the decision sandbox (sandbox/app.py). The browser only ever calls
// /api/playground; SANDBOX_URL stays on the server. The sandbox holds the Token Factory key and
// enforces the budget, rate limits and input validation; this route adds a size cap and a timeout.
import { NextRequest, NextResponse } from "next/server";

export const dynamic = "force-dynamic";

const TIMEOUT_MS = 30_000;
const MAX_BODY = 4_096;

function base(): string | null {
  const url = process.env.SANDBOX_URL?.trim();
  if (!url || !/^https?:\/\/[\w.\-:]+(\/[\w.\-/]*)?$/.test(url)) return null;
  return url.replace(/\/$/, "");
}

const off = () =>
  NextResponse.json({ ok: false, message: "Playground backend is not configured (SANDBOX_URL unset)." }, { status: 503 });

async function forward(url: string, init: RequestInit): Promise<NextResponse> {
  try {
    const res = await fetch(url, { ...init, cache: "no-store", signal: AbortSignal.timeout(TIMEOUT_MS) });
    const body = await res.text();
    return new NextResponse(body, {
      status: res.status,
      headers: { "content-type": res.headers.get("content-type") ?? "application/json", "cache-control": "no-store" },
    });
  } catch (err) {
    const timeout = err instanceof Error && err.name === "TimeoutError";
    console.error(JSON.stringify({ event: "playground.proxy_failed", timeout }));
    return NextResponse.json({ ok: false, message: timeout ? "Playground backend timed out." : "Playground backend unreachable." }, { status: 502 });
  }
}

export async function GET(): Promise<NextResponse> {
  const b = base();
  if (!b) return off();
  return forward(`${b}/health`, { method: "GET" });
}

export async function POST(req: NextRequest): Promise<NextResponse> {
  const b = base();
  if (!b) return off();
  const body = await req.text();
  if (body.length > MAX_BODY) return NextResponse.json({ ok: false, message: "Request too large." }, { status: 413 });
  const ip = req.headers.get("x-forwarded-for")?.split(",")[0]?.trim() || req.headers.get("x-real-ip") || "unknown";
  return forward(`${b}/api/s1/decide`, {
    method: "POST",
    body,
    headers: { "content-type": "application/json", "x-forwarded-for": ip },
  });
}
