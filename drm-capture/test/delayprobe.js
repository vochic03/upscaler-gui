(async () => {
  const m = await import("/js/boxes.js");
  const p = window.__capture.pipeline;
  const out = [];
  for (const delay of [0, 3600, 10000, 7200]) {
    const u = new URL(p.manifestUrl);
    u.searchParams.set("delay", String(delay));
    u.searchParams.set("_", String(Date.now()));
    const r = await fetch("/proxy", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({url: u.href, headers: p.headers})});
    const xml = await r.text();
    const mpd = new DOMParser().parseFromString(xml, "application/xml");
    const sets = Array.from(mpd.getElementsByTagName("AdaptationSet"));
    const vset = sets.find(s => Array.from(s.getElementsByTagName("Representation"))
      .some(r => (r.getAttribute("mimeType")||"").startsWith("video")));
    const reps = Array.from(vset.getElementsByTagName("Representation"))
      .sort((a,b)=>+b.getAttribute("bandwidth")-+a.getAttribute("bandwidth"));
    const st = vset.getElementsByTagName("SegmentTemplate")[0];
    const tpl = st.getAttribute("media")
      .replace("$RepresentationID$", reps[0].getAttribute("id"))
      .replace("$Number%09d$", st.getAttribute("startNumber"));
    const segUrl = new URL(tpl, u.href).href;
    const r2 = await fetch("/proxy", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({url: segUrl, headers: p.headers})});
    const b = new Uint8Array(await r2.arrayBuffer());
    const t = m.fragmentTimes(b);
    out.push({
      delay: delay,
      seg: segUrl.split("/").pop(),
      firstFrag: t.length ? t[0].start : null,
      secs: t.length ? +(t[0].start/12800).toFixed(1) : null,
    });
  }
  return JSON.stringify(out);
})()
