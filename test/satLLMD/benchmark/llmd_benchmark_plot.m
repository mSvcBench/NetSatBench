function T = llmd_benchmark_plot(file, savePrefix, scrapeFile)
% LLMD_BENCHMARK_PLOT  Plot the output of llmd_benchmark.py (--output, one JSON object per line) and, optionally,
% the worker state sampled by scraper.py (--output, one JSON object per scrape).
%
%   T = llmd_benchmark_plot('bench.jsonl')
%   T = llmd_benchmark_plot('bench.jsonl', 'fig/bench')                 % also saves fig/bench_<n>.png
%   T = llmd_benchmark_plot('bench.jsonl', '', 'scrape.json')           % benchmark + scraper figures
%   llmd_benchmark_plot('', '', 'scrape.json')                          % scraper figures only
%
% Returns a table with one row per request (empty if no benchmark file); column 'kind' is "turn" or "summary"
% (the client's summary request when the context is full). Figures 1, 2, 6, 7 include every request, figures 3-5b
% only the turns. Benchmark figures:
%   1. latency (client) and simulated ttft/e2e vs time
%   2. CDF of ttft, e2e and client latency
%   3. context length (and summarisation events) per session vs turn
%   4. prompt tokens: cached vs not cached per turn (first session), and cache hit fraction per turn (first session)
%   5. latency breakdown (queue / prefill / decode) per turn (first session) when the gateway reports it
%   5b. TTFT vs mean inter-token time per turn (first session): 3rd subplot of figure 4, own figure if no cache data
%   6. requests per gateway and latency per gateway (box plot)
%   7. pod usage: requests processed by each pod / total requests (all sessions), grouped and coloured per gateway
% Scraper figures (time = seconds from the first scrape; the benchmark file has no absolute time to align with):
%   8. totals over time: inflight / prefill / decode sequences, completion rate, KV occupancy and hit rate,
%      reachable worker processes
%   9. per worker process vs time (heatmaps, grouped per router): inflight requests and KV cache occupancy
%  10. per worker process totals (grouped and coloured per router): completed requests, KV hit rate, evictions
if nargin < 2, savePrefix = ''; end
if nargin < 3, scrapeFile = ''; end
T = table();  figs = gobjects(0);
if ~isempty(file), [T, figs] = plot_bench(file); end
if ~isempty(scrapeFile), figs = [figs, plot_scrape(scrapeFile)]; end

%% ---------- save ----------
if ~isempty(savePrefix)
    d = fileparts(savePrefix);
    if ~isempty(d) && ~exist(d, 'dir'), mkdir(d); end
    for k = 1:numel(figs)
        exportgraphics(figs(k), sprintf('%s_%d.png', savePrefix, k), 'Resolution', 150);
    end
end
end

function [T, figs] = plot_bench(file)
% Benchmark figures 1-7 (see llmd_benchmark_plot).
%% ---------- load ----------
lines = splitlines(strtrim(fileread(file)));
lines = lines(~cellfun(@isempty, lines));
n = numel(lines);
if n == 0, error('no records in %s', file); end

g = @(s, f, d) getfield_or(s, f, d);
session = zeros(n,1); turn = zeros(n,1); t = zeros(n,1); latency = zeros(n,1);
ctx = zeros(n,1); summarized = false(n,1); promptTok = nan(n,1); cachedTok = nan(n,1);
outTok = nan(n,1); ttft = nan(n,1); e2e = nan(n,1); queue = nan(n,1); prefill = nan(n,1);
decode = nan(n,1); gateway = strings(n,1); pod = strings(n,1); router = strings(n,1); kind = strings(n,1);
for i = 1:n
    r = jsondecode(lines{i});
    kind(i) = string(g(r, 'kind', 'turn'));
    session(i) = r.session;  turn(i) = r.turn;  t(i) = r.time;  latency(i) = r.latency_s;
    ctx(i) = r.context_tokens;  summarized(i) = logical(r.summarized);  gateway(i) = string(r.gateway);
    u = g(r, 'usage', struct());  e = g(r, 'llmd_emu', struct());
    promptTok(i) = g(u, 'prompt_tokens', NaN);  outTok(i) = g(u, 'completion_tokens', NaN);
    pd = g(u, 'prompt_tokens_details', struct());  cachedTok(i) = g(pd, 'cached_tokens', NaN);
    ttft(i) = g(e, 'ttft_s', NaN);  e2e(i) = g(e, 'e2e_s', NaN);
    queue(i) = g(e, 'queue_s', NaN);  prefill(i) = g(e, 'prefill_s', NaN);  decode(i) = g(e, 'decode_s', NaN);
    pod(i) = string(r.llmd_emu.pod); router(i) = string(r.llmd_emu.router);
end
t = t - min(t);                                    % seconds from the first request
isTurn = kind ~= "summary";                        % figures 3-5b: conversation turns only
T = table(kind, session, turn, t, gateway, router, pod, latency, ttft, e2e, queue, prefill, decode, ...
          promptTok, cachedTok, outTok, ctx, summarized, ...
          'VariableNames', {'kind','session','turn','time_s','gateway','router','pod','latency_s','ttft_s','e2e_s','queue_s', ...
          'prefill_s','decode_s','prompt_tokens','cached_tokens','completion_tokens','context_tokens','summarized'});
fprintf('%d requests (%d summaries), %d sessions, %d gateways, %.1f s\n', n, sum(~isTurn), numel(unique(session)), ...
        numel(unique(gateway)), max(t));
fprintf('latency  mean %.3f  p50 %.3f  p95 %.3f  p99 %.3f s\n', mean(latency), prctile(latency,[50 95 99]));
if any(~isnan(ttft)), fprintf('ttft     mean %.3f  p50 %.3f  p95 %.3f s\n', mean(ttft,'omitnan'), prctile(ttft,[50 95])); end

sess = unique(session);
cols = lines_colors(numel(sess));
figs = gobjects(0);

%% ---------- 1. latency vs time ----------
figs(end+1) = figure('Name', 'Latency vs time'); hold on; grid on;
plot(t, latency, '.', 'MarkerSize', 10, 'DisplayName', 'client latency');
if any(~isnan(e2e)),  plot(t, e2e,  '.', 'MarkerSize', 10, 'DisplayName', 'e2e (emulated)'); end
if any(~isnan(ttft)), plot(t, ttft, '.', 'MarkerSize', 10, 'DisplayName', 'ttft (emulated)'); end
xlabel('time (s)'); ylabel('seconds'); title('Request latency vs time'); legend('Location', 'best');

%% ---------- 2. CDF ----------
figs(end+1) = figure('Name', 'Latency CDF'); hold on; grid on;
series = {latency, 'client latency'; e2e, 'e2e (emulated)'; ttft, 'ttft (emulated)'};
for k = 1:size(series, 1)
    x = sort(series{k,1}(~isnan(series{k,1})));
    if ~isempty(x), stairs(x, (1:numel(x))' / numel(x), 'LineWidth', 1.5, 'DisplayName', series{k,2}); end
end
xlabel('seconds'); ylabel('CDF'); title('Latency distribution'); legend('Location', 'southeast');

%% ---------- 3. context length per session ----------
figs(end+1) = figure('Name', 'Context length'); hold on; grid on;
for k = 1:numel(sess)
    m = session == sess(k) & isTurn;  [~, o] = sort(turn(m));
    tt = turn(m);  cc = ctx(m);  tt = tt(o);  cc = cc(o);
    plot(tt, cc, '-', 'Color', cols(k,:), 'LineWidth', 1);
end
s = summarized;
plot(turn(s), ctx(s), 'kv', 'MarkerFaceColor', 'k', 'DisplayName', 'summarised');
xlabel('turn'); ylabel('context (tokens)'); title('Context length per session (v = summarisation)');

%% ---------- 4. prefix cache ----------
hasItl = any(~isnan(ttft)) && any(~isnan(decode)) && any(~isnan(outTok));
if any(~isnan(cachedTok))
    figs(end+1) = figure('Name', 'Prefix cache');
    nr = 2 + hasItl;                                           % 3rd row: TTFT vs inter-token time
    subplot(nr,1,1); hold on; grid on;
    miss = promptTok - cachedTok;
    m1 = session == sess(1) & isTurn;  [mt, o] = sort(turn(m1));   % session 1 only, turns only
    hit = cachedTok(m1);  hit = hit(o);  mis = miss(m1);  mis = mis(o);
    mp = pod(m1);  mp = mp(o);
    mg = gateway(m1);  mg = mg(o);
    chg = find(mp(2:end) ~= mp(1:end-1)) + 1;                  % turns served by a different pod than the previous one
    chgG = find(mg(2:end) ~= mg(1:end-1)) + 1;                 % turns entering through a different gateway
    hb = bar(mt, [hit mis], 'stacked');
    hc = mark_changes(mt, hit + mis, chg,  'rv', 8,  'r');
    hg = mark_changes(mt, hit + mis, chgG, 'bs', 11, 'none');
    legend([hb hc hg], [{'cached', 'not cached'}, repmat({'pod change'}, 1, numel(hc)), repmat({'gateway change'}, 1, numel(hg))], ...
           'Location', 'northwest');
    xlabel('turn'); ylabel('prompt tokens'); title(sprintf('Prompt tokens: cached vs computed (session %d)', sess(1)));
    subplot(nr,1,2); hold on; grid on;
    frac = hit ./ (hit + mis);                                 % session 1, sorted by turn
    hf = plot(mt, frac, '.-', 'MarkerSize', 12);
    hc = mark_changes(mt, frac, chg,  'rv', 8,  'r');
    hg = mark_changes(mt, frac, chgG, 'bs', 11, 'none');
    legend([hf hc hg], [{'cached fraction'}, repmat({'pod change'}, 1, numel(hc)), repmat({'gateway change'}, 1, numel(hg))], ...
           'Location', 'southeast');
    xlabel('turn'); ylabel('cached fraction'); ylim([0 1]); title(sprintf('Cache hit fraction per turn (session %d)', sess(1)));
    if hasItl
        subplot(nr,1,3);
        plot_ttft_itl(mt, ttft(m1), decode(m1), outTok(m1), o, chg, chgG, sess(1));
    end
end

%% ---------- 5. latency breakdown ----------
if any(~isnan(queue)) && any(~isnan(prefill)) && any(~isnan(decode))
    figs(end+1) = figure('Name', 'Latency breakdown');
    m1 = session == sess(1) & isTurn;  [mt, o] = sort(turn(m1));   % session 1 only, turns only
    B = [queue(m1) prefill(m1) decode(m1)];  B = B(o,:);
    mp = pod(m1);  mp = mp(o);  mg = gateway(m1);  mg = mg(o);
    chg  = find(mp(2:end) ~= mp(1:end-1)) + 1;                 % pod changes w.r.t. the previous turn
    chgG = find(mg(2:end) ~= mg(1:end-1)) + 1;                 % gateway changes w.r.t. the previous turn
    hb = bar(mt, B, 'stacked'); grid on; hold on;
    top = sum(B, 2, 'omitnan');
    hc = mark_changes(mt, top, chg,  'rv', 8,  'r');
    hg = mark_changes(mt, top, chgG, 'bs', 11, 'none');
    legend([hb hc hg], [{'queue', 'prefill', 'decode'}, repmat({'pod change'}, 1, numel(hc)), repmat({'gateway change'}, 1, numel(hg))], ...
           'Location', 'northwest');
    xlabel('turn'); ylabel('seconds'); title(sprintf('Emulated latency breakdown per turn (session %d)', sess(1)));
end

%% ---------- 5b. TTFT vs inter-token time (own figure only if there is no prefix-cache figure) ----------
if hasItl && ~any(~isnan(cachedTok))
    figs(end+1) = figure('Name', 'TTFT vs ITL');
    m1 = session == sess(1) & isTurn;  [mt, o] = sort(turn(m1));   % session 1 only, turns only
    mp = pod(m1);  mp = mp(o);  mg = gateway(m1);  mg = mg(o);
    chg  = find(mp(2:end) ~= mp(1:end-1)) + 1;                 % pod changes w.r.t. the previous turn
    chgG = find(mg(2:end) ~= mg(1:end-1)) + 1;                 % gateway changes w.r.t. the previous turn
    plot_ttft_itl(mt, ttft(m1), decode(m1), outTok(m1), o, chg, chgG, sess(1));
end

%% ---------- 6. per gateway ----------
gws = unique(gateway);
if numel(gws) > 0
    figs(end+1) = figure('Name', 'Per gateway');
    subplot(2,1,1);
    cnt = arrayfun(@(x) sum(gateway == x), gws);
    bar(categorical(gws), cnt); grid on; ylabel('requests'); title('Requests per gateway');
    subplot(2,1,2);
    boxplot(latency, cellstr(gateway)); grid on; ylabel('client latency (s)'); title('Latency per gateway');
end

%% ---------- 7. pod usage, grouped per gateway ----------
pods = unique(pod(pod ~= ""));
if numel(pods) > 0
    figs(end+1) = figure('Name', 'Pod usage');
    use = arrayfun(@(x) sum(pod == x), pods) / n;              % requests processed by the pod / total requests
    pgw = arrayfun(@(x) mode_str(gateway(pod == x)), pods);    % gateway of the pod = the one that sent it most requests
    [~, o] = sort(natural_key(pgw) + "|" + natural_key(pods)); % group by gateway, natural order (sat2 < sat51 < sat100)
    pods = pods(o);  use = use(o);  pgw = pgw(o);
    gwo = unique(pgw, 'stable');  ng = numel(gwo);
    [~, gi] = ismember(pgw, gwo);
    x = (1:numel(pods))' + (gi - 1);                           % one empty slot between gateway groups
    if ng <= 7, C = lines_colors(ng); else, C = turbo(ng); end  % not lines(): shadowed by the variable 'lines'
    hold on; grid on;
    hb = bar(x, use * 100, 0.8, 'FaceColor', 'flat');  hb.CData = C(gi, :);
    text(x, use * 100, compose('%.1f%%', use * 100), 'HorizontalAlignment', 'left', ...
         'VerticalAlignment', 'middle', 'Rotation', 90, 'FontSize', 7);
    hl = gobjects(ng, 1);
    for k = 1:ng, hl(k) = patch(NaN, NaN, C(k, :)); end        % legend entries, one per gateway
    legend(hl, cellstr(gwo), 'Location', 'eastoutside'); legend('boxoff');
    xticks(x); xticklabels(cellstr(pods)); xtickangle(90); xlim([0 max(x) + 1]);
    ylabel('pod usage (% of requests)'); ylim([0 max(use * 100) * 1.25]);
    title(sprintf('Pod usage per gateway (%d requests, all sessions)', n));
end

end

function v = getfield_or(s, f, d)
if isstruct(s) && isfield(s, f) && ~isempty(s.(f)), v = s.(f); else, v = d; end
end

function c = lines_colors(k)
c = lines(max(k, 1));
end

function h = mark_changes(mt, y, chg, style, sz, face)
% Marker on each turn listed in chg (e.g. where the serving pod or the gateway changes w.r.t. the previous turn).
% Returns the marker handle (for the legend), or empty if there is no change.
h = gobjects(0);
if isempty(chg), return; end
h = plot(mt(chg), y(chg), style, 'MarkerSize', sz, 'MarkerFaceColor', face, 'LineWidth', 1.5, 'LineStyle', 'none');
end

function plot_ttft_itl(mt, tf, dec, out, o, chg, chgG, s1)
% TTFT (left axis, with pod/gateway change marks) vs mean inter-token time (right axis) per turn, in the current axes.
tf = tf(o);
itl = dec ./ max(out - 1, 1);  itl = itl(o);                % decode_s covers the tokens after the 1st one
yyaxis right;  hi = plot(mt, itl * 1e3, 's-', 'MarkerSize', 5, 'LineWidth', 1.2); ylabel('mean inter-token time (ms)');
yyaxis left;   hold on;                                     % marks drawn on the TTFT curve (left axis)
ht = plot(mt, tf * 1e3, 'o-', 'MarkerSize', 5, 'LineWidth', 1.2); ylabel('TTFT (ms)');
hc = mark_changes(mt, tf * 1e3, chg,  'rv', 8,  'r');
hg = mark_changes(mt, tf * 1e3, chgG, 'bs', 11, 'none');
grid on; xlabel('turn');
legend([ht hi hc hg], [{'TTFT', 'inter-token time'}, repmat({'pod change'}, 1, numel(hc)), repmat({'gateway change'}, 1, numel(hg))], ...
       'Location', 'northwest');
title(sprintf('TTFT vs inter-token time per turn (session %d)', s1));
end

function m = mode_str(s)
% Most frequent string in s.
[u, ~, k] = unique(s);  [~, im] = max(accumarray(k, 1));  m = u(im);
end

function k = natural_key(s)
% Sort key with zero-padded numbers, so that "sat2" < "sat51" < "sat100".
k = string(regexprep(cellstr(s), '(\d+)', '${sprintf(''%09d'', str2double($1))}'));
end

function figs = plot_scrape(file)
% Scraper figures 8-10 (see llmd_benchmark_plot). One JSON line per scrape: {"time", "elapsed_s", "workers": [...]},
% each worker process {"url", "node", "port", "router", "ok", <worker /debug/state> when ok}.
lines = splitlines(strtrim(fileread(file)));
lines = lines(~cellfun(@isempty, lines));
ns = numel(lines);
if ns == 0, error('no scrapes in %s', file); end

F = {'inflight', 'completed', 'running_prefill', 'running_decode', 'kv_pinned_blocks', 'blocks_used', ...
     'capacity_blocks', 'hit_blocks', 'lookup_blocks', 'evictions'};
key = containers.Map('KeyType', 'char', 'ValueType', 'double');
urls = strings(0,1);  wnode = strings(0,1);  wrouter = strings(0,1);
ts = zeros(ns,1);  V = struct();
for f = F, V.(f{1}) = nan(0, ns); end
for j = 1:ns
    r = jsondecode(lines{j});
    ts(j) = r.time;
    W = r.workers;  if isstruct(W), W = num2cell(W); end     % struct array if all records have the same fields
    for i = 1:numel(W)
        w = W{i};  u = char(w.url);
        if ~isKey(key, u)
            key(u) = numel(urls) + 1;  urls(end+1,1) = string(u);  wnode(end+1,1) = string(w.node);
            rt = getfield_or(w, 'router', '');  if isempty(rt), rt = ''; end
            wrouter(end+1,1) = string(rt);
            for f = F, V.(f{1})(end+1, :) = NaN; end
        end
        if ~w.ok, continue; end
        k = key(u);
        for f = F, V.(f{1})(k, j) = getfield_or(w, f{1}, NaN); end
    end
end
ts = ts - ts(1);
nw = numel(urls);
ok = ~isnan(V.inflight);
fprintf('scrape: %d samples, %d worker processes, %.1f s, %.1f%% of the samples reachable\n', ...
        ns, nw, ts(end), 100 * mean(ok(:)));

% group per router, natural order (sat2 < sat51 < sat100)
lab = wnode + ":" + string(regexprep(cellstr(urls), '.*:(\d+)$', '$1'));
[~, o] = sort(natural_key(wrouter) + "|" + natural_key(lab));
urls = urls(o);  lab = lab(o);  wrouter = wrouter(o);  ok = ok(o, :);
for f = F, V.(f{1}) = V.(f{1})(o, :); end
rts = unique(wrouter, 'stable');  nr = numel(rts);
[~, gi] = ismember(wrouter, rts);
if nr <= 7, C = lines_colors(nr); else, C = turbo(nr); end

% counter increments between consecutive reachable samples of the same process (restart = counter back to 0)
[dComp, dtComp] = counter_inc(V.completed, ts);
dHit = counter_inc(V.hit_blocks, ts);  dLook = counter_inc(V.lookup_blocks, ts);  dEv = counter_inc(V.evictions, ts);
anyOk = any(ok, 1)';
tot = @(X) nan_if(sum(fillmissing(X, 'previous', 2), 1, 'omitnan')', ~anyOk);   % gauges: unreachable process = its
                                                                                 % last reachable value
win = max(1, round(10 / median(diff(ts))));                 % ~10 s moving window
figs = gobjects(0);

%% ---------- 8. totals over time ----------
figs(end+1) = figure('Name', 'Workers: totals vs time');
tl = tiledlayout(4, 1, 'TileSpacing', 'compact');
ax(1) = nexttile; hold on; grid on;
plot(ts, tot(V.inflight), 'DisplayName', 'inflight');
plot(ts, tot(V.running_prefill), 'DisplayName', 'running prefill');
plot(ts, tot(V.running_decode), 'DisplayName', 'running decode');
ylabel('sequences'); legend('Location', 'northwest'); title('Load (sum over worker processes)');
ax(2) = nexttile; hold on; grid on;
rate = sum(dComp ./ dtComp, 1, 'omitnan')';                 % each process' rate since its previous reachable sample
rate(~anyOk) = NaN;
plot(ts, rate, ':', 'Color', [.6 .6 .6], 'DisplayName', 'per sample');
plot(ts, movmean(rate, win, 'omitnan'), 'LineWidth', 1.5, 'DisplayName', sprintf('%d-sample mean', win));
ylabel('requests/s'); legend('Location', 'northwest'); title('Completion rate');
ax(3) = nexttile; hold on; grid on;
plot(ts, tot(V.blocks_used) ./ tot(V.capacity_blocks), 'DisplayName', 'KV occupancy');
plot(ts, tot(V.kv_pinned_blocks) ./ tot(V.capacity_blocks), 'DisplayName', 'KV pinned');
h = movsum(sum(dHit, 1, 'omitnan')', win);  l = movsum(sum(dLook, 1, 'omitnan')', win);
plot(ts, nan_if(h ./ l, l == 0), 'LineWidth', 1.5, 'DisplayName', sprintf('hit rate (%d-sample window)', win));
plot(ts, tot(V.hit_blocks) ./ tot(V.lookup_blocks), '--', 'DisplayName', 'hit rate (cumulative)');
ylim([0 1]); ylabel('fraction'); legend('Location', 'northwest'); title('KV cache (all worker processes)');
ax(4) = nexttile; grid on;
stairs(ts, sum(ok, 1)', 'LineWidth', 1.2); ylim([0 nw + 1]);
ylabel('processes'); title(sprintf('Reachable worker processes (of %d)', nw));
xlabel(tl, 'time from the first scrape (s)'); linkaxes(ax, 'x'); xlim(ax(1), [0 ts(end)]);

%% ---------- 9. per worker process vs time ----------
figs(end+1) = figure('Name', 'Workers: per process vs time');
tl = tiledlayout(1, 2, 'TileSpacing', 'compact');
tg = linspace(0, ts(end), min(2000, ns));                   % uniform time grid (scrapes are not evenly spaced)
ig = interp1(ts, 1:ns, tg, 'nearest');
H = {V.inflight(:, ig), 'inflight requests'; 100 * V.blocks_used(:, ig) ./ V.capacity_blocks(:, ig), 'KV occupancy (%)'};
for k = 1:2
    nexttile;
    imagesc(tg, 1:nw, H{k,1}, 'AlphaData', ~isnan(H{k,1}));  % unreachable = background
    set(gca, 'YDir', 'normal', 'Color', [1 1 1], 'TickLength', [0 0]);
    cb = colorbar;  cb.Label.String = H{k,2};
    yticks(1:nw);  yticklabels(cellstr(lab));  set(gca, 'FontSize', 7);
    if k == 2, yticklabels({}); end
    hold on;
    for b = find(diff(gi))' , yline(b + 0.5, 'k-', 'LineWidth', 1); end   % router group separators
    title(H{k,2}, 'FontSize', 10);
end
colormap(turbo);
xlabel(tl, 'time from the first scrape (s)');
title(tl, 'Per worker process (grouped per router; white = unreachable)');

%% ---------- 10. per worker process totals ----------
figs(end+1) = figure('Name', 'Workers: per process totals');
tl = tiledlayout(3, 1, 'TileSpacing', 'compact');
first = @(X) arrayfun(@(i) first_ok(X(i,:)), (1:nw)');
compl = first(V.completed) + sum(dComp, 2, 'omitnan');
hits  = first(V.hit_blocks) + sum(dHit, 2, 'omitnan');
looks = first(V.lookup_blocks) + sum(dLook, 2, 'omitnan');
evs   = first(V.evictions) + sum(dEv, 2, 'omitnan');
x = (1:nw)' + (gi - 1);                                      % one empty slot between router groups
Y = {compl, 'completed requests'; 100 * hits ./ looks, 'KV hit rate (%)'; evs, 'KV evictions (blocks)'};
for k = 1:3
    nexttile; hold on; grid on;
    hb = bar(x, Y{k,1}, 0.8, 'FaceColor', 'flat');  hb.CData = C(gi, :);
    ylabel(Y{k,2});  xlim([0 max(x) + 1]);  ylim([0 max([Y{k,1}; 0], [], 'omitnan') * 1.1 + eps]);  xticks(x);
    if k < 3, xticklabels({}); else, xticklabels(cellstr(lab)); xtickangle(90); end
    set(gca, 'FontSize', 7);
    if k == 1
        hl = gobjects(nr, 1);
        for g = 1:nr, hl(g) = patch(NaN, NaN, C(g, :)); end
        legend(hl, cellstr(rts), 'Location', 'eastoutside'); legend('boxoff');
    end
end
title(tl, sprintf('Per worker process totals over %.0f s, grouped per router (%d completed requests)', ts(end), sum(compl)));
end

function [d, dt] = counter_inc(X, ts)
% Increments of the cumulative counters X (process x sample, NaN = unreachable) between consecutive reachable
% samples of the same process, placed at the later sample; dt = time since that previous reachable sample.
% A decrease means the process restarted (counters back to 0): the increment is then the new value.
d = nan(size(X));  dt = nan(size(X));
for i = 1:size(X, 1)
    k = find(~isnan(X(i,:)));
    if numel(k) < 2, continue; end
    v = X(i,k);  dv = diff(v);  dv(dv < 0) = v([false dv < 0]);
    d(i, k(2:end)) = dv;  dt(i, k(2:end)) = diff(ts(k))';
end
end

function v = first_ok(x)
v = x(find(~isnan(x), 1));  if isempty(v), v = 0; end
end

function x = nan_if(x, m)
x(m) = NaN;
end
