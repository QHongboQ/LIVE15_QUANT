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
import { useCallback, useEffect, useRef, useState } from 'react';

export type ChartPoint = { time: string; value: number };
export type ChartSeries = { id: string; label: string; color: string; points?: ChartPoint[]; history?: ChartPoint[]; livePoints?: ChartPoint[] };

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
export function FinancialChart({ series, reference, height = 300, ariaLabel, resetKey, contractStart, showLatestMarker = true, hideRightPriceScale = false }: { series: ChartSeries[]; reference?: number | null; height?: number; ariaLabel: string; resetKey?: string; contractStart?: string | null; showLatestMarker?: boolean; hideRightPriceScale?: boolean }) {
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
  const normalizedHistory = useRef<Map<string, { source: ChartPoint[]; points: { time: UTCTimestamp; value: number }[] }>>(new Map());
  const previousResetKey = useRef(resetKey);
  const initialHeight = useRef(height);
  const tooltip = useRef<HTMLDivElement>(null);
  const autoFollow = useRef(true);
  const [manualView, setManualView] = useState(false);
  const defaultViewport = useCallback(() => {
    if (!chart.current || !contractStart) return chart.current?.timeScale().fitContent();
    const from = timestamp(contractStart);
    const latest = [...renderedLivePoints.current.values(), ...renderedPoints.current.values()].flat().at(-1)?.time;
    if (latest && latest >= from) chart.current.timeScale().setVisibleRange({ from: from as Time, to: latest as Time });
    else chart.current.timeScale().fitContent();
  }, [contractStart]);

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
      timeScale: { borderColor: '#272637', rightOffsetPixels: 48, timeVisible: true, secondsVisible: true, lockVisibleTimeRangeOnResize: true, tickMarkFormatter: (time: Time) => new Date(Number(time) * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) },
      handleScroll: { vertTouchDrag: false },
    });
    chart.current = instance;
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
    const observer = new ResizeObserver(() => instance.timeScale().applyOptions({ rightOffsetPixels: Math.max(36, Math.round((host.current?.clientWidth ?? 400) * 0.1)) }));
    const hostElement = host.current;
    observer.observe(hostElement);
    const suspendAutoFollow = () => { autoFollow.current = false; setManualView(true); };
    hostElement.addEventListener('wheel', suspendAutoFollow, { passive: true });
    hostElement.addEventListener('pointerdown', suspendAutoFollow);
    const markersForCleanup = seriesMarkers.current;
    const renderedLiveForCleanup = renderedLivePoints.current;
    const normalizedHistoryForCleanup = normalizedHistory.current;
    return () => { hostElement.removeEventListener('wheel', suspendAutoFollow); hostElement.removeEventListener('pointerdown', suspendAutoFollow); instance.timeScale().unsubscribeVisibleLogicalRangeChange(onRangeChange); observer.disconnect(); for (const markers of markersForCleanup.values()) markers.detach(); markersForCleanup.clear(); if (referenceLine.current && referenceSeries.current) referenceSeries.current.removePriceLine(referenceLine.current); referenceLine.current = undefined; referenceSeries.current = undefined; referenceLinePrice.current = undefined; instance.remove(); chart.current = undefined; seriesMap.clear(); renderedLiveForCleanup.clear(); normalizedHistoryForCleanup.clear(); };
  }, []);

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
    chart.current.timeScale().applyOptions({ rightOffsetPixels: Math.max(36, Math.round((host.current?.clientWidth ?? 400) * 0.1)) });
  }, [series, reference, height, resetKey, contractStart, showLatestMarker, hideRightPriceScale, defaultViewport]);

  const empty = series.every((item) => (item.history ?? item.points ?? []).length === 0 && (item.livePoints ?? []).length === 0);
  return <div className="financial-chart" style={{ height }} aria-label={ariaLabel}><div ref={host} className="financial-chart-host" />{manualView && <button className="chart-reset" onClick={() => { autoFollow.current = true; setManualView(false); defaultViewport(); }}>Reset view</button>}{empty && <div className="chart-empty" style={{ position: 'absolute', inset: 0, display: 'grid', placeItems: 'center', padding: 24, color: '#9994a5', textAlign: 'center' }}>No persisted history is available for this read-only projection.</div>}<div ref={tooltip} className="chart-tooltip" /></div>;
}
