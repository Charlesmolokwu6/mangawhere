/* Storyteller mode's OCR, run on the reader's own device (index.html,
   phone narration). The server has a fraction of one CPU and 512MB, far
   too little to read a chapter's speech bubbles; a phone has several fast
   cores and the pages are already on it. So the same PP-OCRv4 models the
   server-side storyteller uses (server/storyteller.py, via RapidOCR) run
   here through onnxruntime-web, with RapidOCR's pre- and post-processing
   ported.

   Webtoon pages are strips ~760x14000px. Read whole, they're shrunk until
   the text is unreadable (and need >2GB); so text is found in overlapping
   slices of each strip, and every line is kept by exactly one slice: the
   one its centre falls in, away from that slice's overlapping edges.
   Measured in Chromium on a 12-strip chapter: ~16s a strip on one core,
   about half of it finding text and half reading it.

   Messages in:  {type:"init"}, {type:"ocr", id, url}
   Messages out: {type:"progress", label}, {type:"ready"},
                 {type:"lines", id, lines:[[box, text, score], ...], frontier, last}
                   after each slice: the lines it found, and how far down
                   the page has been read (everything above is final),
                 {type:"result", id, ms} once the page is done,
                 {type:"error", id?, message}
   Each box is four [x, y] corners in the page image's own pixels. Lines
   come slice by slice so the voice can start on the first bubbles of a
   tall strip instead of waiting for all of it (~20s). */

var ORT_BASE = "https://cdn.jsdelivr.net/npm/onnxruntime-web@1.19.2/dist/";
var MODEL_BASE = "https://cdn.jsdelivr.net/npm/@gutenye/ocr-models@1.4.2/assets/";
var MODEL_CACHE = "mw-ocr-v1";   // models and runtime, kept after the first download

var SLICE = 1600;          // slice height, in the page's own pixels
var OVERLAP = 240;         // taller than any line, so each line is whole in some slice
var DET_SCALE = 1;         // finding text works at the page's own size...
var MAX_DET_WIDTH = 1280;  // (wide pages are scaled down to this to find it)
var DET_LIMIT = 736;       // RapidOCR's limit_side_len (type "min")
var DET_THRESH = 0.3, BOX_THRESH = 0.5, UNCLIP = 1.6, MIN_SIZE = 3;
var REC_HEIGHT = 48, REC_MIN_WIDTH = 320, REC_BATCH = 6;
var TEXT_SCORE = 0.5;

importScripts(ORT_BASE + "ort.wasm.min.js");
ort.env.wasm.wasmPaths = ORT_BASE;
// Several threads need cross-origin isolation, which GitHub Pages can't
// turn on; one thread still works everywhere.
ort.env.wasm.numThreads = self.crossOriginIsolated
  ? Math.min(4, (self.navigator && navigator.hardwareConcurrency) || 1) : 1;

var det = null, rec = null, chars = null, ready = null, timing = null;

function say(label) { postMessage({type: "progress", label: label}); }

// Model files are kept in the Cache API after the first download, so a
// phone fetches the ~15MB only once.
function cachedFetch(url) {
  if (!self.caches) return fetch(url).then(check);
  return caches.open(MODEL_CACHE).then(function (cache) {
    return cache.match(url).then(function (hit) {
      if (hit) return hit;
      return fetch(url).then(check).then(function (res) {
        return cache.put(url, res.clone()).then(function () { return res; }, function () { return res; });
      });
    });
  });
}
function check(res) {
  if (!res.ok) throw new Error("Couldn't download the text reader (" + res.status + ").");
  return res;
}

function init() {
  if (ready) return ready;
  say(self.caches ? "Getting the text reader ready…" : "Downloading the text reader…");
  var opts = {executionProviders: ["wasm"], graphOptimizationLevel: "all"};
  ready = Promise.all([
    cachedFetch(MODEL_BASE + "ch_PP-OCRv4_det_infer.onnx").then(function (r) { return r.arrayBuffer(); }),
    cachedFetch(MODEL_BASE + "ch_PP-OCRv4_rec_infer.onnx").then(function (r) { return r.arrayBuffer(); }),
    cachedFetch(MODEL_BASE + "ppocr_keys_v1.txt").then(function (r) { return r.text(); }),
    // The 11MB runtime too: left to the browser's own cache, a phone short
    // on space drops it and downloads it again.
    cachedFetch(ORT_BASE + "ort-wasm-simd-threaded.wasm").then(function (r) { return r.arrayBuffer(); })
  ]).then(function (files) {
    ort.env.wasm.wasmBinary = files[3];
    // RapidOCR's character list: the keys file, then a space; index 0 is
    // the CTC blank.
    chars = ["blank"].concat(files[2].replace(/\r/g, "").split("\n").filter(function (c, i, a) {
      return !(i === a.length - 1 && c === "");
    }), [" "]);
    return Promise.all([
      ort.InferenceSession.create(files[0], opts),
      ort.InferenceSession.create(files[1], opts)
    ]);
  }).then(function (s) {
    det = s[0]; rec = s[1];
  });
  ready.catch(function () { ready = null; });
  return ready;
}

// ---- detection -----------------------------------------------------------

function canvas(w, h) {
  var c = new OffscreenCanvas(w, h);
  return {c: c, ctx: c.getContext("2d", {willReadFrequently: true})};
}

// Pixels as the models expect them: BGR planes, scaled to [-1, 1].
function toTensor(rgba, w, h, outW) {
  outW = outW || w;
  var plane = h * outW, data = new Float32Array(3 * plane);
  for (var y = 0; y < h; y++) {
    for (var x = 0; x < w; x++) {
      var i = (y * w + x) * 4, o = y * outW + x;
      data[o] = rgba[i + 2] / 127.5 - 1;
      data[plane + o] = rgba[i + 1] / 127.5 - 1;
      data[2 * plane + o] = rgba[i] / 127.5 - 1;
    }
  }
  return data;
}

function detect(slice) {
  var w = slice.c.width, h = slice.c.height;
  var ratio = Math.min(w, h) < DET_LIMIT ? DET_LIMIT / Math.min(w, h) : 1;
  var rw = Math.max(32, Math.round(w * ratio / 32) * 32);
  var rh = Math.max(32, Math.round(h * ratio / 32) * 32);
  var input = canvas(rw, rh);
  input.ctx.drawImage(slice.c, 0, 0, rw, rh);
  var rgba = input.ctx.getImageData(0, 0, rw, rh).data;
  var feeds = {};
  feeds[det.inputNames[0]] = new ort.Tensor("float32", toTensor(rgba, rw, rh), [1, 3, rh, rw]);
  return det.run(feeds).then(function (out) {
    return boxesFromMap(out[det.outputNames[0]].data, rw, rh, w / rw, h / rh);
  });
}

// DB post-processing: threshold, 2x2 dilation, connected regions, a score
// for each, then each box grown by RapidOCR's unclip distance. Boxes are
// axis-aligned (lettering in these pages is horizontal).
function boxesFromMap(pred, w, h, sx, sy) {
  var n = w * h, mask = new Uint8Array(n);
  for (var i = 0; i < n; i++) if (pred[i] > DET_THRESH) mask[i] = 1;
  var dil = new Uint8Array(n);
  for (var y = 0; y < h; y++) {
    for (var x = 0; x < w; x++) {
      var p = y * w + x;
      if (mask[p] || (x > 0 && mask[p - 1]) || (y > 0 && (mask[p - w] || (x > 0 && mask[p - w - 1])))) dil[p] = 1;
    }
  }
  var seen = new Uint8Array(n), stack = new Int32Array(n), boxes = [];
  for (var s = 0; s < n; s++) {
    if (!dil[s] || seen[s]) continue;
    var top = 0, x0 = w, y0 = h, x1 = 0, y1 = 0;
    stack[top++] = s; seen[s] = 1;
    while (top) {
      var q = stack[--top], qx = q % w, qy = (q - qx) / w;
      if (qx < x0) x0 = qx; if (qx > x1) x1 = qx;
      if (qy < y0) y0 = qy; if (qy > y1) y1 = qy;
      for (var dy = -1; dy <= 1; dy++) {
        var ny = qy + dy;
        if (ny < 0 || ny >= h) continue;
        for (var dx = -1; dx <= 1; dx++) {
          var nx = qx + dx;
          if (nx < 0 || nx >= w) continue;
          var r = ny * w + nx;
          if (dil[r] && !seen[r]) { seen[r] = 1; stack[top++] = r; }
        }
      }
    }
    var bw = x1 - x0, bh = y1 - y0;
    if (Math.min(bw, bh) < MIN_SIZE) continue;
    var sum = 0;
    for (var yy = y0; yy <= y1; yy++) for (var xx = x0; xx <= x1; xx++) sum += pred[yy * w + xx];
    var score = sum / ((bw + 1) * (bh + 1));
    if (score < BOX_THRESH) continue;
    var d = (bw * bh) * UNCLIP / (2 * (bw + bh));
    var ux0 = x0 - d, uy0 = y0 - d, ux1 = x1 + d, uy1 = y1 + d;
    if (Math.min(ux1 - ux0, uy1 - uy0) < MIN_SIZE + 2) continue;
    boxes.push([
      Math.max(0, Math.round(ux0 * sx)), Math.max(0, Math.round(uy0 * sy)),
      Math.round(ux1 * sx), Math.round(uy1 * sy)
    ]);
  }
  return boxes;
}

// ---- recognition ---------------------------------------------------------

// ...while reading it works from each line cut straight out of the page
// and scaled to the model's 48px height, which keeps tightly kerned
// lettering's word gaps (reading a whole page at its own size ran words
// together).
function recognize(bmp, boxes) {
  var W = bmp.width, H = bmp.height, crops = [];
  boxes.forEach(function (b) {
    var x0 = Math.max(0, Math.min(b[0], W - 1)), y0 = Math.max(0, Math.min(b[1], H - 1));
    var cw = Math.min(b[2], W) - x0, ch = Math.min(b[3], H) - y0;
    // Tall, narrow boxes are vertical text or art, not lettering to read.
    if (cw < 2 || ch < 2 || ch / cw >= 1.5) return;
    crops.push({box: b, x: x0, y: y0, w: cw, h: ch, ratio: cw / ch});
  });
  crops.sort(function (a, b) { return a.ratio - b.ratio; });
  var batches = [];
  for (var i = 0; i < crops.length; i += REC_BATCH) batches.push(crops.slice(i, i + REC_BATCH));
  var out = [];
  return batches.reduce(function (chain, batch) {
    return chain.then(function () { return recBatch(bmp, batch, out); });
  }, Promise.resolve()).then(function () { return out; });
}

function recBatch(bmp, batch, out) {
  var maxRatio = REC_MIN_WIDTH / REC_HEIGHT;
  batch.forEach(function (c) { maxRatio = Math.max(maxRatio, c.ratio); });
  var imgW = Math.ceil(REC_HEIGHT * maxRatio), plane = REC_HEIGHT * imgW;
  var data = new Float32Array(batch.length * 3 * plane);   // zero = mid grey, RapidOCR's padding
  var scratch = canvas(imgW, REC_HEIGHT);
  batch.forEach(function (c, k) {
    var rw = Math.min(imgW, Math.ceil(REC_HEIGHT * c.ratio));
    scratch.ctx.clearRect(0, 0, imgW, REC_HEIGHT);
    scratch.ctx.drawImage(bmp, c.x, c.y, c.w, c.h, 0, 0, rw, REC_HEIGHT);
    var t = toTensor(scratch.ctx.getImageData(0, 0, rw, REC_HEIGHT).data, rw, REC_HEIGHT, imgW);
    data.set(t, k * 3 * plane);
  });
  var feeds = {};
  feeds[rec.inputNames[0]] = new ort.Tensor("float32", data, [batch.length, 3, REC_HEIGHT, imgW]);
  return rec.run(feeds).then(function (res) {
    var t = res[rec.outputNames[0]], steps = t.dims[1], classes = t.dims[2], p = t.data;
    batch.forEach(function (c, k) {
      var text = "", probs = [], last = -1;
      for (var s = 0; s < steps; s++) {
        var base = (k * steps + s) * classes, best = 0, bestP = -1;
        for (var j = 0; j < classes; j++) if (p[base + j] > bestP) { bestP = p[base + j]; best = j; }
        if (best !== 0 && best !== last) { text += chars[best] || ""; probs.push(bestP); }
        last = best;
      }
      var score = probs.length ? probs.reduce(function (a, b) { return a + b; }, 0) / probs.length : 0;
      if (text.trim() && score >= TEXT_SCORE) out.push({box: c.box, text: text, score: score});
    });
  });
}

// ---- a whole page ----------------------------------------------------------

function ocrPage(blob, onSlice) {
  return createImageBitmap(blob).then(function (bmp) {
    var W = bmp.width, H = bmp.height, scale = Math.min(DET_SCALE, MAX_DET_WIDTH / W);
    var lines = [], top = 0;
    timing = {det: 0, rec: 0, slices: 0};
    function next() {
      var bottom = Math.min(top + SLICE, H), sh = bottom - top;
      var slice = canvas(Math.round(W * scale), Math.max(1, Math.round(sh * scale)));
      slice.ctx.imageSmoothingQuality = "high";
      slice.ctx.drawImage(bmp, 0, top, W, sh, 0, 0, slice.c.width, slice.c.height);
      var t0 = Date.now(), t1;
      var lo = top + (top > 0 ? OVERLAP / 2 : 0), hi = bottom < H ? bottom - OVERLAP / 2 : H + 1;
      var last = bottom >= H;
      return detect(slice).then(function (boxes) {
        t1 = Date.now(); timing.det += t1 - t0;
        // Into the page's own pixels; only the lines this slice owns.
        boxes = boxes.map(function (b) {
          return [b[0] / scale, b[1] / scale + top, b[2] / scale, b[3] / scale + top];
        }).filter(function (b) { var cy = (b[1] + b[3]) / 2; return cy >= lo && cy < hi; });
        return recognize(bmp, boxes);
      }).then(function (found) {
        timing.rec += Date.now() - t1; timing.slices++;
        var fresh = found.map(function (f) {
          var b = f.box;
          return [[[b[0], b[1]], [b[2], b[1]], [b[2], b[3]], [b[0], b[3]]], f.text, f.score];
        });
        lines = lines.concat(fresh);
        onSlice(fresh, last ? H : hi, last);
        if (last) { if (bmp.close) bmp.close(); return lines; }
        top = bottom - OVERLAP;
        return next();
      });
    }
    return next();
  });
}

onmessage = function (e) {
  var m = e.data || {};
  if (m.type === "init") {
    init().then(function () { postMessage({type: "ready"}); },
      function (err) { postMessage({type: "error", message: String(err && err.message || err)}); });
  } else if (m.type === "ocr") {
    var started = Date.now();
    init().then(function () { return fetch(m.url); }).then(function (res) {
      if (!res.ok) throw new Error("Couldn't load page (" + res.status + ").");
      return res.blob();
    }).then(function (blob) {
      return ocrPage(blob, function (lines, frontier, last) {
        postMessage({type: "lines", id: m.id, lines: lines, frontier: frontier, last: last});
      });
    }).then(function () {
      postMessage({type: "result", id: m.id, ms: Date.now() - started, timing: timing});
    }, function (err) {
      postMessage({type: "error", id: m.id, message: String(err && err.message || err)});
    });
  }
};
