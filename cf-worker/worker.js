export default {
  async fetch(request, env) {
    if (request.method !== "POST") {
      return new Response("Method Not Allowed", { status: 405 });
    }

    let body;
    try {
      body = await request.json();
    } catch {
      return new Response("Bad Request", { status: 400 });
    }

    const { instance_id, version, platform, series_bucket, languages, webhook, merge_volumes } = body;

    if (typeof instance_id !== "string" || !/^[0-9a-f-]{36}$/.test(instance_id)) {
      return new Response("Bad Request", { status: 400 });
    }

    const now = new Date().toISOString();
    const langs = Array.isArray(languages)
      ? JSON.stringify(languages.filter(l => typeof l === "string").slice(0, 20))
      : "[]";

    await env.DB.prepare(`
      INSERT INTO instances
        (instance_id, version, platform, series_bucket, languages, webhook, merge_volumes, first_seen, last_seen)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(instance_id) DO UPDATE SET
        version       = excluded.version,
        platform      = excluded.platform,
        series_bucket = excluded.series_bucket,
        languages     = excluded.languages,
        webhook       = excluded.webhook,
        merge_volumes = excluded.merge_volumes,
        last_seen     = excluded.last_seen
    `).bind(
      instance_id,
      String(version ?? "unknown").slice(0, 32),
      String(platform ?? "").slice(0, 32),
      String(series_bucket ?? "").slice(0, 10),
      langs,
      webhook ? 1 : 0,
      merge_volumes ? 1 : 0,
      now,
      now,
    ).run();

    return new Response("OK", { status: 200 });
  },
};
