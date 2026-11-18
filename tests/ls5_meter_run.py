"""Run the existing LS-2 acquisition against the separate LS-5 comparison case."""
from tests import local_speed_meter_run

if __name__ == "__main__":
    local_speed_meter_run.SELECTOR = "tests/integrations/test_ls5_meter.py"
    raise SystemExit(local_speed_meter_run.main())
