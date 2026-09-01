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
export type ChartSeries = { id: string; label: string; color: string; points: ChartPoint[] };

const timestamp = (value: string): UTCTimestamp => Math.floor(Date.parse(value) / 1000) as UTCTimestamp;

function clean(points: ChartPoint[]) {
  return points
    .filter((point) => Number.isFinite(point.value) && Number.isFinite(Date.parse(point.time)))
    .map((point) => ({ time: timestamp(point.time), value: point.value }))
    .sort((left, right) => Number(left.time) - Number(right.time));
}

/** A thin local adapter around the Apache-2.0 TradingView Lightweight Charts dependency. */
export function FinancialChart({ series, reference, height = 300, ariaLabel }: { series: ChartSeries[]; reference?: number | null; height?: number; ariaLabel: string }) {
  const host = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi>();
  const chartSeries = useRef<Map<string, ISeriesApi<'Line'>>>(new Map());
  const seriesMarkers = useRef<Map<string, ISeriesMarkersPluginApi<Time>>>(new Map());
  const referenceLine = useRef<IPriceLine>();
  const referenceSeries = useRef<ISeriesApi<'Line'>>();
  const seriesRef = useRef(series);
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
    return () => { observer.disconnect(); for (const markers of markersForCleanup.values()) markers.detach(); markersForCleanup.clear(); instance.remove(); chart.current = undefined; seriesMap.clear(); };
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
    }
    for (const item of series) {
      let line = chartSeries.current.get(item.id);
      if (!line) {
        line = chart.current.addSeries(LineSeries, { color: item.color, lineWidth: 2, crosshairMarkerRadius: 4, crosshairMarkerBorderColor: '#101018', crosshairMarkerBackgroundColor: item.color, lastValueVisible: true, priceLineVisible: false });
        chartSeries.current.set(item.id, line);
      }
      const points = clean(item.points);
      line.setData(points);
      if (item === series[0]) {
        let markers = seriesMarkers.current.get(item.id);
        if (!markers) {
          markers = createSeriesMarkers<Time>(line);
          seriesMarkers.current.set(item.id, markers);
        }
        markers.setMarkers(points.length ? [{ time: points.at(-1)!.time as Time, position: 'inBar', color: item.color, shape: 'circle', text: 'NOW', size: 2 }] : []);
      }
    }
    if (referenceLine.current && referenceSeries.current) {
      referenceSeries.current.removePriceLine(referenceLine.current);
      referenceLine.current = undefined;
      referenceSeries.current = undefined;
    }
    if (reference != null && Number.isFinite(reference) && series[0]) {
      const line = chartSeries.current.get(series[0].id);
      referenceLine.current = line?.createPriceLine({ price: reference, color: '#a797e8', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: 'TARGET' });
      referenceSeries.current = line;
    }
    chart.current.timeScale().fitContent();
    chart.current.timeScale().applyOptions({ rightOffsetPixels: Math.max(36, Math.round((host.current?.clientWidth ?? 400) * 0.1)) });
  }, [series, reference, height]);

  const empty = series.every((item) => item.points.length === 0);
  return <div className="financial-chart" style={{ height }} aria-label={ariaLabel}><div ref={host} className="financial-chart-host" />{empty && <div className="chart-empty" style={{ position: 'absolute', inset: 0, display: 'grid', placeItems: 'center', padding: 24, color: '#9994a5', textAlign: 'center' }}>No persisted history is available for this read-only projection.</div>}<div ref={tooltip} className="chart-tooltip" /></div>;
}
