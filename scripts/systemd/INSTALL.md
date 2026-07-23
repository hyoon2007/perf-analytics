# systemd Install

## 1) Copy unit files

```bash
sudo cp /opt/perf-analytics/scripts/systemd/perf-report-monitor.service /etc/systemd/system/
sudo cp /opt/perf-analytics/scripts/systemd/perf-report-once.service /etc/systemd/system/
```

## 2) Reload and enable monitor

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now perf-report-monitor.service
```

## 3) Check status/logs

```bash
systemctl status perf-report-monitor.service
journalctl -u perf-report-monitor.service -f
```

## 4) Run one file manually as unit

Use escaped filename as instance:

```bash
sudo systemctl start perf-report-once@sample_data_1-21_181_firstcontentfulpaint_iter5_win1_inter5.csv.service
```
