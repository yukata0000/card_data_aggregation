"""
Streamlit版では Django Admin は利用しないが、
`dashbords.apps.DashbordsConfig.ready()` が `.admin` を import するため
モジュールだけは存在させる。

（必要になったら元プロジェクトの `dashbords/admin.py` を持ってきてください）
"""


