import {
  createChart,
  createSeriesMarkers,
  LineSeries,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type Time,
  type UTCTimestamp,
  type WhitespaceData,
} from 'lightweight-charts';
import { useCallback, useEffect, useRef, useState } from 'react';

export type ChartPoint = { time: string; value: number };
export type ChartSeries = { id: string; label: string; color: string; points?: ChartPoint[]; history?: ChartPoint[]; livePoints?: ChartPoint[] };

const timestamp = (value: string): UTCTimestamp => Math.floor(Date.parse(value) / 1000) as UTCTimestamp;
const TIME_DOMAIN_MAX_POINTS = 720;

/** Browser-local presentation time only; it never creates observations or requests data. */
export function usePresentationClock(enabled = true) {
  const [presentationNow, setPresentationNow] = useState(() => Date.now());
  useEffect(() => {
    if (!enabled) return;
    const timer = window.setInterval(() => setPresentationNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [enabled]);
  return presentationNow;
}

function timeDomainWhitespace(from: UTCTimestamp, to: UTCTimestamp): WhitespaceData<Time>[] {
  if (from >= to) return [{ time: from as Time }];
  const count = Math.min(TIME_DOMAIN_MAX_POINTS, Number(to) - Number(from) + 1);
  return Array.from({ length: count }, (_, index) => ({ time: Math.round(Number(from) + (Number(to) - Number(from)) * index / (count - 1)) as Time }));
}

function clean(points: ChartPoint[]) {
  const byTime = new Map<number, { time: UTCTimestamp; value: number }>();
  for (const point of points) {
    if (!Number.isFinite(point.value) || !Number.isFinite(Date.parse(point.time))) continue;
    const normalized = { time: timestamp(point.time), value: point.value };
    byTime.set(Number(normalized.time), normalized);
  }
  return [...byTime.values()].sort((left, right) => Number(left.time) - Number(right.time));
}

function combinePoints(left: { time: UTCTimestamp; value: number }[], right: { time: UTCTimestamp; value: number }[]) {
  const byTime = new Map<number, { time: UTCTimestamp; value: number }>();
  for (const point of [...left, ...right]) byTime.set(Number(point.time), point);
  return [...byTime.values()].sort((first, second) => Number(first.time) - Number(second.time));
}

function samePoint(left: { time: UTCTimestamp; value: number }, right: { time: UTCTimestamp; value: number }) {
  return left.time === right.time && left.value === right.value;
}

function incrementalPoints(previous: { time: UTCTimestamp; value: number }[], next: { time: UTCTimestamp; value: number }[]) {
  if (!previous.length || !next.length) return undefined;
  const latestPoint = next.at(-1)!;
  if (next.length === previous.length && previous.slice(0, -1).every((point, index) => samePoint(point, next[index])) && previous.at(-1)!.time === latestPoint.time) return [latestPoint];
  if (next.length === previous.length && latestPoint.time > previous.at(-1)!.time) {
    for (let offset = 0; offset < previous.length; offset += 1) {
      const overlap = Math.min(previous.length - offset, next.length);
      if (previous.slice(offset, offset + overlap).every((point, index) => samePoint(point, next[index]))) {
        const added = next.slice(overlap);
        if (added.length) return added;
      }
    }
  }
  if (next.length > previous.length && previous.every((point, index) => samePoint(point, next[index]))) return next.slice(previous.length);
  return undefined;
}

/** A thin local adapter around the Apache-2.0 TradingView Lightweight Charts dependency. */
export function FinancialChart({ series, reference, height = 300, ariaLabel, resetKey, contractStart, contractEnd, showLatestMarker = true, hideRightPriceScale = false }: { series: ChartSeries[]; reference?: number | null; height?: number; ariaLabel: string; resetKey?: string; contractStart?: string | null; contractEnd?: string | null; showLatestMarker?: boolean; hideRightPriceScale?: boolean }) {
  const host = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi>();
  const chartSeries = useRef<Map<string, ISeriesApi<'Line'>>>(new Map());
  const seriesMarkers = useRef<Map<string, ISeriesMarkersPluginApi<Time>>>(new Map());
  const referenceLine = useRef<IPriceLine>();
  const referenceSeries = useRef<ISeriesApi<'Line'>>();
  const timeDomainSeries = useRef<ISeriesApi<'Line'>>();
  const renderedTimeDomain = useRef<string>();
  const referenceLinePrice = useRef<number>();
  const seriesRef = useRef(series);
  const renderedPoints = useRef<Map<string, { time: UTCTimestamp; value: number }[]>>(new Map());
  const renderedLivePoints = useRef<Map<string, { time: UTCTimestamp; value: number }[]>>(new Map());
  const normalizedHistory = useRef<Map<string, { source: ChartPoint[]; points: { time: UTCTimestamp; value: number }[] }>>(new Map());
  const previousResetKey = useRef(resetKey);
  const initialHeight = useRef(height);
  const tooltip = useRef<HTMLDivElement>(null);
  const autoFollow = useRef(true);
  const [manualView, setManualView] = useState(false);
  const presentationNow = usePresentationClock(Boolean(contractStart && contractEnd));
  const presentationNowRef = useRef(presentationNow);
  const defaultViewport = useCallback(() => {
    if (!chart.current) return;
    if (!contractStart || !contractEnd) return chart.current.timeScale().fitContent();
    const from = timestamp(contractStart);
    const domainEnd = timestamp(contractEnd);
    const to = Math.max(Number(from), Math.min(Math.floor(presentationNowRef.current / 1000), Number(domainEnd))) as UTCTimestamp;
    const domainKey = `${from}:${to}`;
    if (renderedTimeDomain.current !== domainKey) {
      // This bounded carrier is presentation-only whitespace, never an observation.
      timeDomainSeries.current?.setData(timeDomainWhitespace(from, to));
      renderedTimeDomain.current = domainKey;
    }
    chart.current.timeScale().setVisibleRange({ from: from as Time, to: to as Time });
  }, [contractEnd, contractStart]);

  useEffect(() => {
    seriesRef.current = series;
  }, [series]);

  useEffect(() => {
    if (!host.current) return;
    const instance = createChart(host.current, {
      autoSize: true,
      height: initialHeight.current,
      layout: { background: { color: '#101018' }, textColor: '#9393a5', fontFamily: 'Inter, ui-sans-serif, system-ui, sans-serif' },
      grid: { vertLines: { color: '#171724' }, horzLines: { color: '#1c1c2a' } },
      crosshair: { vertLine: { color: '#7164a8', labelBackgroundColor: '#51428f' }, horzLine: { color: '#7164a8', labelBackgroundColor: '#51428f' } },
      rightPriceScale: { borderColor: '#272637', scaleMargins: { top: 0.12, bottom: 0.12 } },
      timeScale: { borderColor: '#272637', rightOffset: 0, rightOffsetPixels: 0, shiftVisibleRangeOnNewBar: false, timeVisible: true, secondsVisible: false, lockVisibleTimeRangeOnResize: true, tickMarkFormatter: (time: Time) => new Date(Number(time) * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) },
      handleScroll: { vertTouchDrag: false },
    });
    chart.current = instance;
    timeDomainSeries.current = instance.addSeries(LineSeries, { lineVisible: false, lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false });
    const onRangeChange = () => { if (autoFollow.current) return; setManualView(true); };
    instance.timeScale().subscribeVisibleLogicalRangeChange(onRangeChange);
    instance.subscribeCrosshairMove((event) => {
      if (!tooltip.current) return;
      if (!event.point || !event.time || !event.seriesData.size) { tooltip.current.style.opacity = '0'; return; }
      const values = seriesRef.current.map((item) => {
        const row = event.seriesData.get(seriesMap.get(item.id)!);
        return row && 'value' in row ? `${item.label} ${Number(row.value).toLocaleString(undefined, { maximumFractionDigits: 4 })}` : null;
      }).filter(Boolean);
      tooltip.current.textContent = values.join(' · ');
      tooltip.current.style.opacity = '1';
      tooltip.current.style.left = `${Math.min(event.point.x + 12, (host.current?.clientWidth ?? 0) - 180)}px`;
      tooltip.current.style.top = `${Math.max(10, event.point.y - 26)}px`;
    });
    const seriesMap = chartSeries.current;
    const observer = new ResizeObserver(() => instance.timeScale().applyOptions({ rightOffset: 0, rightOffsetPixels: 0 }));
    const hostElement = host.current;
    observer.observe(hostElement);
    const suspendAutoFollow = () => { autoFollow.current = false; setManualView(true); };
    hostElement.addEventListener('wheel', suspendAutoFollow, { passive: true });
    hostElement.addEventListener('pointerdown', suspendAutoFollow);
    const markersForCleanup = seriesMarkers.current;
    const renderedLiveForCleanup = renderedLivePoints.current;
    const normalizedHistoryForCleanup = normalizedHistory.current;
    return () => { hostElement.removeEventListener('wheel', suspendAutoFollow); hostElement.removeEventListener('pointerdown', suspendAutoFollow); instance.timeScale().unsubscribeVisibleLogicalRangeChange(onRangeChange); observer.disconnect(); for (const markers of markersForCleanup.values()) markers.detach(); markersForCleanup.clear(); if (referenceLine.current && referenceSeries.current) referenceSeries.current.removePriceLine(referenceLine.current); referenceLine.current = undefined; referenceSeries.current = undefined; referenceLinePrice.current = undefined; timeDomainSeries.current = undefined; renderedTimeDomain.current = undefined; instance.remove(); chart.current = undefined; seriesMap.clear(); renderedLiveForCleanup.clear(); normalizedHistoryForCleanup.clear(); };
  }, []);

  useEffect(() => {
    presentationNowRef.current = presentationNow;
    if (autoFollow.current) defaultViewport();
  }, [presentationNow, defaultViewport]);

  useEffect(() => {
    if (!chart.current) return;
    chart.current.applyOptions({ height });
    chart.current.priceScale('right').applyOptions({ visible: !hideRightPriceScale });
    const activeIds = new Set(series.map((item) => item.id));
    for (const [id, line] of chartSeries.current) {
      if (activeIds.has(id)) continue;
      seriesMarkers.current.get(id)?.detach();
      seriesMarkers.current.delete(id);
      chart.current.removeSeries(line);
      chartSeries.current.delete(id);
      renderedPoints.current.delete(id);
      renderedLivePoints.current.delete(id);
      normalizedHistory.current.delete(id);
    }
    const resetRequired = previousResetKey.current !== resetKey;
    previousResetKey.current = resetKey;
    for (const item of series) {
      let line = chartSeries.current.get(item.id);
      if (!line) {
        line = chart.current.addSeries(LineSeries, { color: item.color, lineWidth: 2, crosshairMarkerRadius: 4, crosshairMarkerBorderColor: '#101018', crosshairMarkerBackgroundColor: item.color, lastValueVisible: true, priceLineVisible: false });
        chartSeries.current.set(item.id, line);
      }
      const hasImmutableHistory = item.history !== undefined;
      const historySource = item.history ?? item.points ?? [];
      const cachedHistory = normalizedHistory.current.get(item.id);
      const historyChanged = !cachedHistory || cachedHistory.source !== historySource;
      const historicalPoints = historyChanged ? clean(historySource) : cachedHistory.points;
      if (historyChanged) normalizedHistory.current.set(item.id, { source: historySource, points: historicalPoints });
      let markerPoint: { time: UTCTimestamp; value: number } | undefined;
      if (hasImmutableHistory) {
        const livePoints = clean(item.livePoints ?? []);
        const previousLive = renderedLivePoints.current.get(item.id);
        if (!previousLive || resetRequired || historyChanged) {
          const combined = combinePoints(historicalPoints, livePoints);
          line.setData(combined);
        } else if (livePoints.length !== previousLive.length || livePoints.some((point, index) => !samePoint(point, previousLive[index]))) {
          const newPoints = incrementalPoints(previousLive, livePoints);
          if (newPoints) {
            for (const point of newPoints) line.update(point);
          } else {
            const combined = combinePoints(historicalPoints, livePoints);
            line.setData(combined);
          }
        }
        renderedLivePoints.current.set(item.id, livePoints);
        markerPoint = livePoints.at(-1) ?? historicalPoints.at(-1);
      } else {
        const points = historicalPoints;
        const previous = renderedPoints.current.get(item.id);
        if (!previous || resetRequired || historyChanged) {
          line.setData(points);
        } else if (points.length !== previous.length || points.some((point, index) => !samePoint(point, previous[index]))) {
          const newPoints = incrementalPoints(previous, points);
          if (newPoints) {
            for (const point of newPoints) line.update(point);
          }
          else {
            line.setData(points);
          }
        }
        renderedPoints.current.set(item.id, points);
        markerPoint = points.at(-1);
      }
      if (item === series[0] && showLatestMarker) {
        let markers = seriesMarkers.current.get(item.id);
        if (!markers) {
          markers = createSeriesMarkers<Time>(line);
          seriesMarkers.current.set(item.id, markers);
        }
        markers.setMarkers(markerPoint ? [{ time: markerPoint.time as Time, position: 'inBar', color: item.color, shape: 'circle', size: 1 }] : []);
      } else if (item === series[0]) {
        seriesMarkers.current.get(item.id)?.detach();
        seriesMarkers.current.delete(item.id);
      }
    }
    const nextReferenceSeries = series[0] ? chartSeries.current.get(series[0].id) : undefined;
    if (referenceSeries.current !== nextReferenceSeries) {
      if (referenceLine.current && referenceSeries.current) referenceSeries.current.removePriceLine(referenceLine.current);
      referenceLine.current = undefined;
      referenceSeries.current = undefined;
      referenceLinePrice.current = undefined;
    }
    if (reference != null && Number.isFinite(reference) && nextReferenceSeries) {
      if (!referenceLine.current) {
        referenceLine.current = nextReferenceSeries.createPriceLine({ price: reference, color: '#a797e8', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: 'TARGET' });
        referenceSeries.current = nextReferenceSeries;
        referenceLinePrice.current = reference;
      } else if (referenceLinePrice.current !== reference) {
        referenceLine.current.applyOptions({ price: reference });
        referenceLinePrice.current = reference;
      }
    } else if (referenceLine.current && referenceSeries.current) {
      referenceSeries.current.removePriceLine(referenceLine.current);
      referenceLine.current = undefined;
      referenceSeries.current = undefined;
      referenceLinePrice.current = undefined;
    }
    if (autoFollow.current) defaultViewport();
  }, [series, reference, height, resetKey, contractStart, contractEnd, showLatestMarker, hideRightPriceScale, defaultViewport]);

  const empty = series.every((item) => (item.history ?? item.points ?? []).length === 0 && (item.livePoints ?? []).length === 0);
  return <div className="financial-chart" style={{ height }} aria-label={ariaLabel}><div ref={host} className="financial-chart-host" />{manualView && <button className="chart-reset" onClick={() => { autoFollow.current = true; setManualView(false); defaultViewport(); }}>Reset view</button>}{empty && <div className="chart-empty" style={{ position: 'absolute', inset: 0, display: 'grid', placeItems: 'center', padding: 24, color: '#9994a5', textAlign: 'center' }}>No persisted history is available for this read-only projection.</div>}<div ref={tooltip} className="chart-tooltip" /></div>;
}

const PORTFOLIO_VIEWBOX_WIDTH = 1000;
const PORTFOLIO_TICK_COUNT = 6;

const portfolioTimeLabel = (time: number, span: number) => new Date(time * 1000).toLocaleString([], span >= 48 * 60 * 60 ? { month: 'short', day: 'numeric' } : { hour: '2-digit', minute: '2-digit' });
const portfolioValueLabel = (value: number) => new Intl.NumberFormat(undefined, { style: 'currency', currency: 'USD', maximumFractionDigits: Math.abs(value) >= 100 ? 0 : 2 }).format(value);

/** A fixed-canvas Portfolio renderer with exact calendar x-domain and actual observations only. */
export function PortfolioEquityChart({ points, from, to, height = 300 }: { points: ChartPoint[]; from: string; to: string; height?: number }) {
  const domainFrom = timestamp(from);
  const domainTo = timestamp(to);
  const actual = clean(points).filter((item) => item.time >= domainFrom && item.time <= domainTo);
  const left = 12;
  const right = 18;
  const top = 16;
  const bottom = 34;
  const plotWidth = PORTFOLIO_VIEWBOX_WIDTH - left - right;
  const plotHeight = height - top - bottom;
  const span = Math.max(1, Number(domainTo) - Number(domainFrom));
  const values = actual.map((item) => item.value);
  const minimum = values.length ? Math.min(...values) : 0;
  const maximum = values.length ? Math.max(...values) : 0;
  const margin = values.length ? Math.max((maximum - minimum) * 0.08, Math.abs(maximum) * 0.02, 1) : 1;
  const yMinimum = minimum - margin;
  const yMaximum = maximum + margin;
  const ySpan = Math.max(1, yMaximum - yMinimum);
  const x = (time: number) => left + (time - Number(domainFrom)) / span * plotWidth;
  const y = (value: number) => top + (yMaximum - value) / ySpan * plotHeight;
  const path = actual.map((item, index) => `${index ? 'L' : 'M'}${x(Number(item.time)).toFixed(2)} ${y(item.value).toFixed(2)}`).join(' ');
  const ticks = Array.from({ length: PORTFOLIO_TICK_COUNT }, (_, index) => Number(domainFrom) + span * index / (PORTFOLIO_TICK_COUNT - 1));
  const yTicks = Array.from({ length: 3 }, (_, index) => yMinimum + (yMaximum - yMinimum) * index / 2);
  return <div className="portfolio-equity-chart" style={{ height }} aria-label={`Account equity history with ${actual.length} actual samples`} data-domain-from={from} data-domain-to={to} data-actual-samples={actual.length}><svg viewBox={`0 0 ${PORTFOLIO_VIEWBOX_WIDTH} ${height}`} role="img" aria-label="Actual account equity history"><g className="portfolio-grid">{ticks.map((item) => <line key={`x-${item}`} x1={x(item)} x2={x(item)} y1={top} y2={top + plotHeight} />)}{yTicks.map((item) => <line key={`y-${item}`} x1={left} x2={left + plotWidth} y1={y(item)} y2={y(item)} />)}</g>{path && <path className="portfolio-equity-line" d={path} />}{ticks.map((item, index) => <text className="portfolio-x-label" key={`label-${item}`} x={x(item)} y={height - 9} textAnchor={index === 0 ? 'start' : index === PORTFOLIO_TICK_COUNT - 1 ? 'end' : 'middle'}>{portfolioTimeLabel(item, span)}</text>)}{yTicks.map((item) => <text className="portfolio-y-label" key={`value-${item}`} x={PORTFOLIO_VIEWBOX_WIDTH - right} y={y(item) - 5} textAnchor="end">{portfolioValueLabel(item)}</text>)}</svg>{!actual.length && <div className="chart-empty">No actual account observations fall in this calendar range.</div>}</div>;
}
