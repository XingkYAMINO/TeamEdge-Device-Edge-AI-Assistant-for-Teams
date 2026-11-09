def test_homepage_and_local_assets_are_available(client):
    homepage = client.get("/")
    stylesheet = client.get("/static/styles.css")
    script = client.get("/static/app.js")

    assert homepage.status_code == 200
    assert "text/html" in homepage.headers["content-type"]
    assert "本地工作区文件清单" in homepage.text
    assert 'href="/static/styles.css"' in homepage.text
    assert 'src="/static/app.js"' in homepage.text
    assert "cdn" not in homepage.text.lower()
    assert stylesheet.status_code == 200
    assert "text/css" in stylesheet.headers["content-type"]
    assert script.status_code == 200
    assert "javascript" in script.headers["content-type"]


def test_frontend_uses_text_nodes_for_untrusted_scan_data(client):
    script = client.get("/static/app.js").text
    assert "textContent" in script
    assert ".innerHTML" not in script
    assert "已扫描，尚未读取" in script
    assert "按文件名或路径筛选" in client.get("/").text


def test_model_connection_page_and_local_assets_are_available(client):
    page = client.get("/model-connections")
    script = client.get("/static/model-connections.js")
    stylesheet = client.get("/static/model-connections.css")

    assert page.status_code == 200
    assert "模型连接设置" in page.text
    assert "保存配置不会自动连接模型" in page.text
    assert "/static/model-connections.js" in page.text
    assert script.status_code == 200
    assert stylesheet.status_code == 200
    assert ".innerHTML" not in script.text
    assert "localStorage" not in script.text
    assert "sessionStorage" not in script.text
    assert "textContent" in script.text


def test_agent_page_and_local_assets_are_available(client):
    page = client.get("/agent")
    script = client.get("/static/agent.js")
    stylesheet = client.get("/static/agent.css")
    assert page.status_code == 200
    assert "工作区问答" in page.text
    assert "问题和必要的文档片段会发送" in page.text
    assert script.status_code == 200
    assert stylesheet.status_code == 200
    assert ".innerHTML" not in script.text
    assert "textContent" in script.text
