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
} from 'lightweight-charts';
import { useEffect, useRef } from 'react';

export type ChartPoint = { time: string; value: number };
export type ChartSeries = { id: string; label: string; color: string; points?: ChartPoint[]; history?: ChartPoint[]; livePoints?: ChartPoint[] };
const LIVE_TAIL_COMPACTION_MAX_POINTS = 128;

const timestamp = (value: string): UTCTimestamp => Math.floor(Date.parse(value) / 1000) as UTCTimestamp;

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

function incrementalPoint(previous: { time: UTCTimestamp; value: number }[], next: { time: UTCTimestamp; value: number }[]) {
  if (!previous.length || !next.length) return undefined;
  const latestPoint = next.at(-1)!;
  if (next.length === previous.length + 1 && previous.every((point, index) => samePoint(point, next[index]))) return latestPoint;
  if (next.length === previous.length && previous.length && previous.slice(0, -1).every((point, index) => samePoint(point, next[index])) && previous.at(-1)!.time === latestPoint.time) return latestPoint;
  if (next.length === previous.length && latestPoint.time > previous.at(-1)!.time) {
    const divergence = previous.findIndex((point, index) => !samePoint(point, next[index]));
    if (divergence >= 0 && previous.slice(divergence + 1).every((point, index) => samePoint(point, next[divergence + index]))) return latestPoint;
  }
  return undefined;
}

/** A thin local adapter around the Apache-2.0 TradingView Lightweight Charts dependency. */
export function FinancialChart({ series, reference, height = 300, ariaLabel, resetKey }: { series: ChartSeries[]; reference?: number | null; height?: number; ariaLabel: string; resetKey?: string }) {
  const host = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi>();
  const chartSeries = useRef<Map<string, ISeriesApi<'Line'>>>(new Map());
  const seriesMarkers = useRef<Map<string, ISeriesMarkersPluginApi<Time>>>(new Map());
  const referenceLine = useRef<IPriceLine>();
  const referenceSeries = useRef<ISeriesApi<'Line'>>();
  const referenceLinePrice = useRef<number>();
  const seriesRef = useRef(series);
  const renderedPoints = useRef<Map<string, { time: UTCTimestamp; value: number }[]>>(new Map());
  const renderedLivePoints = useRef<Map<string, { time: UTCTimestamp; value: number }[]>>(new Map());
  const renderedDataPointCounts = useRef<Map<string, number>>(new Map());
  const normalizedHistory = useRef<Map<string, { source: ChartPoint[]; points: { time: UTCTimestamp; value: number }[] }>>(new Map());
  const previousResetKey = useRef(resetKey);
  const initialHeight = useRef(height);
  const tooltip = useRef<HTMLDivElement>(null);

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
      timeScale: { borderColor: '#272637', rightOffsetPixels: 48, timeVisible: true, secondsVisible: false, lockVisibleTimeRangeOnResize: true },
      handleScroll: { vertTouchDrag: false },
    });
    chart.current = instance;
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
    const observer = new ResizeObserver(() => instance.timeScale().applyOptions({ rightOffsetPixels: Math.max(36, Math.round((host.current?.clientWidth ?? 400) * 0.1)) }));
    observer.observe(host.current);
    const markersForCleanup = seriesMarkers.current;
    const renderedLiveForCleanup = renderedLivePoints.current;
    const renderedCountsForCleanup = renderedDataPointCounts.current;
    const normalizedHistoryForCleanup = normalizedHistory.current;
    return () => { observer.disconnect(); for (const markers of markersForCleanup.values()) markers.detach(); markersForCleanup.clear(); if (referenceLine.current && referenceSeries.current) referenceSeries.current.removePriceLine(referenceLine.current); referenceLine.current = undefined; referenceSeries.current = undefined; referenceLinePrice.current = undefined; instance.remove(); chart.current = undefined; seriesMap.clear(); renderedLiveForCleanup.clear(); renderedCountsForCleanup.clear(); normalizedHistoryForCleanup.clear(); };
  }, []);

  useEffect(() => {
    if (!chart.current) return;
    chart.current.applyOptions({ height });
    const activeIds = new Set(series.map((item) => item.id));
    for (const [id, line] of chartSeries.current) {
      if (activeIds.has(id)) continue;
      seriesMarkers.current.get(id)?.detach();
      seriesMarkers.current.delete(id);
      chart.current.removeSeries(line);
      chartSeries.current.delete(id);
      renderedPoints.current.delete(id);
      renderedLivePoints.current.delete(id);
      renderedDataPointCounts.current.delete(id);
      normalizedHistory.current.delete(id);
    }
    const resetRequired = previousResetKey.current !== resetKey;
    previousResetKey.current = resetKey;
    let fitRequired = resetRequired;
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
          renderedDataPointCounts.current.set(item.id, combined.length);
          fitRequired = true;
        } else if (livePoints.length !== previousLive.length || livePoints.some((point, index) => !samePoint(point, previousLive[index]))) {
          const latestPoint = incrementalPoint(previousLive, livePoints);
          const currentCount = renderedDataPointCounts.current.get(item.id) ?? historicalPoints.length + previousLive.length;
          const replacement = latestPoint && previousLive.at(-1)?.time === latestPoint.time;
          if (latestPoint && (replacement || currentCount < historicalPoints.length + LIVE_TAIL_COMPACTION_MAX_POINTS)) {
            line.update(latestPoint);
            if (!replacement) renderedDataPointCounts.current.set(item.id, currentCount + 1);
          } else {
            const combined = combinePoints(historicalPoints, livePoints);
            line.setData(combined);
            renderedDataPointCounts.current.set(item.id, combined.length);
            if (!latestPoint) fitRequired = true;
          }
        }
        renderedLivePoints.current.set(item.id, livePoints);
        markerPoint = livePoints.at(-1) ?? historicalPoints.at(-1);
      } else {
        const points = historicalPoints;
        const previous = renderedPoints.current.get(item.id);
        if (!previous || resetRequired || historyChanged) {
          line.setData(points);
          renderedDataPointCounts.current.set(item.id, points.length);
          fitRequired = true;
        } else if (points.length !== previous.length || points.some((point, index) => !samePoint(point, previous[index]))) {
          const latestPoint = incrementalPoint(previous, points);
          if (latestPoint) {
            line.update(latestPoint);
            if (latestPoint.time !== previous.at(-1)?.time) renderedDataPointCounts.current.set(item.id, (renderedDataPointCounts.current.get(item.id) ?? previous.length) + 1);
          }
          else {
            line.setData(points);
            renderedDataPointCounts.current.set(item.id, points.length);
            fitRequired = true;
          }
        }
        renderedPoints.current.set(item.id, points);
        markerPoint = points.at(-1);
      }
      if (item === series[0]) {
        let markers = seriesMarkers.current.get(item.id);
        if (!markers) {
          markers = createSeriesMarkers<Time>(line);
          seriesMarkers.current.set(item.id, markers);
        }
        markers.setMarkers(markerPoint ? [{ time: markerPoint.time as Time, position: 'inBar', color: item.color, shape: 'circle', text: 'NOW', size: 2 }] : []);
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
    if (fitRequired) chart.current.timeScale().fitContent();
    chart.current.timeScale().applyOptions({ rightOffsetPixels: Math.max(36, Math.round((host.current?.clientWidth ?? 400) * 0.1)) });
  }, [series, reference, height, resetKey]);

  const empty = series.every((item) => (item.history ?? item.points ?? []).length === 0 && (item.livePoints ?? []).length === 0);
  return <div className="financial-chart" style={{ height }} aria-label={ariaLabel}><div ref={host} className="financial-chart-host" />{empty && <div className="chart-empty" style={{ position: 'absolute', inset: 0, display: 'grid', placeItems: 'center', padding: 24, color: '#9994a5', textAlign: 'center' }}>No persisted history is available for this read-only projection.</div>}<div ref={tooltip} className="chart-tooltip" /></div>;
}
