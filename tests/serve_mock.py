# Runs the dashboard server with the same mocked HTTP as mock2.py, so add-provider tests hit fakes.
import sys, runpy
sys.argv=["mock2.py"]
from pathlib import Path
src=(Path(__file__).resolve().parent / "mock2.py").read_text()
# reuse fakes only: execute up to the line that builds cfg
head=src.split("cfgp=D+\"/config.yaml\"")[0].replace('shutil.rmtree(D, ignore_errors=True); os.makedirs(D)','os.makedirs(D, exist_ok=True)')
head=head.replace('open(D+"/config.yaml","w").write(cfg_txt)','pass')
head=head.replace('hd=sqlite3.connect(D+"/hermes_state.db")','hd=sqlite3.connect(":memory:")')
exec(compile(head,"mockhead","exec"))
os.environ["AIMETER_DASHBOARD_TOKEN"]="t"
aimon.main(["-c",D+"/config.yaml","serve","--port","8799"])
