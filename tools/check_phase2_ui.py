"""Exercise the isolated DSH renderer; records are temporary verification actions."""
import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright
from campus_radar.storage import Store, default_db
from campus_radar.digest import preferences

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / 'outputs/phase2-qualification'


def ready(page):
    page.get_by_text('正在读取本地数据库…', exact=True).wait_for(state='hidden', timeout=20000)
    assert page.get_by_role('alert').count() == 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('stage', choices=['start', 'restart', 'discovery'])
    args = parser.parse_args()
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp('http://127.0.0.1:19222')
        page = browser.contexts[0].pages[0]
        page.get_by_role('button', name='校园雷达', exact=True).click()
        page.get_by_role('heading', name='校园雷达', exact=True).wait_for()
        ready(page)
        OUT.joinpath(f'ui-{args.stage}-before.txt').write_text(page.locator('main.campus-radar').aria_snapshot())
        if args.stage == 'start':
            with Store(default_db()) as store:
                initial = {'preferences': preferences(store), 'favorites': [dict(r) for r in store.db.execute('SELECT * FROM favorites')], 'feedback': [dict(r) for r in store.db.execute('SELECT * FROM feedback')]}
            if not OUT.joinpath('ui-initial-personal-state.json').exists():
                OUT.joinpath('ui-initial-personal-state.json').write_text(json.dumps(initial, ensure_ascii=False, indent=2))
            page.get_by_role('button', name='全部活动', exact=True).click()
            ready(page)
            OUT.joinpath('ui-filter-controls.txt').write_text(page.locator('main.campus-radar').aria_snapshot())
            page.get_by_label('关键词', exact=True).fill('AI')
            page.get_by_role('combobox', name='校区', exact=True).select_option('闵行')
            page.get_by_label('日期从', exact=True).fill('2026-11-01')
            page.get_by_label('到', exact=True).fill('2026-11-30')
            page.get_by_role('button', name='搜索／筛选', exact=True).click()
            ready(page)
            articles = page.locator('main.campus-radar article')
            assert articles.count() > 0
            filtered_count = articles.count()
            card = articles.first
            title = card.get_by_role('heading', level=3).inner_text()
            card.get_by_role('button', name='查看详情与证据', exact=True).click()
            card.locator('details').wait_for()
            assert card.locator('details .body').first.inner_text()
            assert card.get_by_role('link').first.get_attribute('href').startswith('https://')
            if card.get_by_role('button', name='收藏', exact=True).count():
                card.get_by_role('button', name='收藏', exact=True).click()
            card.get_by_role('button', name='取消收藏', exact=True).wait_for()
            card.get_by_role('combobox', name='反馈', exact=True).select_option('known')
            ready(page)
            page.get_by_role('button', name='我的收藏', exact=True).click()
            ready(page)
            page.get_by_role('heading', name=title, level=3, exact=True).wait_for()
            assert page.get_by_role('combobox', name='反馈', exact=True).first.input_value() == 'known'
            page.screenshot(path=str(ROOT / 'output/playwright/phase2-favorite.png'), full_page=True)
            page.get_by_role('button', name='偏好与定时', exact=True).click()
            ready(page)
            page.get_by_label('主题（逗号分隔）', exact=True).fill('AI，机器学习')
            with page.expect_response(lambda response: response.request.method == 'POST' and '/api/campus-radar/preferences-set' in response.url):
                page.get_by_role('button', name='保存偏好', exact=True).click()
            ready(page)
            with Store(default_db()) as store:
                saved = store.event_listing(favorites=True)['events']
                event = next(e for e in saved if e['title'] == title)
                assert event['feedback'] == 'known'
                assert preferences(store)['themes'] == ['AI', '机器学习']
            OUT.joinpath('ui-saved-test-event.json').write_text(json.dumps({'event_id': event['id'], 'title': title}, ensure_ascii=False))
            page.get_by_role('button', name='更新全部来源', exact=True).click()
            page.get_by_text('更新已触发，进度会自动刷新。', exact=True).wait_for()
            page.get_by_role('button', name='更新全部来源', exact=True).wait_for(state='visible')
            assert page.get_by_role('button', name='更新全部来源', exact=True).is_disabled()
            OUT.joinpath('ui-start-result.json').write_text(json.dumps({'filtered_count': filtered_count, 'saved_title': title, 'detail_body_visible': True, 'favorite_saved': True, 'explicit_feedback_saved': True, 'preferences_saved': True, 'real_update_triggered': True}, ensure_ascii=False, indent=2))
        elif args.stage == 'restart':
            saved = json.loads(OUT.joinpath('ui-saved-test-event.json').read_text())
            page.get_by_role('button', name='我的收藏', exact=True).click()
            ready(page)
            page.get_by_role('heading', name=saved['title'], level=3, exact=True).wait_for()
            assert page.get_by_role('combobox', name='反馈', exact=True).first.input_value() == 'known'
            page.get_by_role('button', name='偏好与定时', exact=True).click()
            ready(page)
            assert page.get_by_label('主题（逗号分隔）', exact=True).input_value() == 'AI，机器学习'
            page.screenshot(path=str(ROOT / 'output/playwright/phase2-restart.png'), full_page=True)
            OUT.joinpath('ui-restart-result.json').write_text(json.dumps({'favorite_after_restart': True, 'feedback_after_restart': True, 'preferences_after_restart': True}))
        else:
            page.get_by_role('button', name='在线补漏', exact=True).click()
            ready(page)
            OUT.joinpath('ui-discovery-controls.txt').write_text(page.locator('main.campus-radar').aria_snapshot())
            page.get_by_label('官网检索关键词', exact=True).fill('AI')
            page.get_by_role('button', name='检索官网', exact=True).click()
            page.get_by_text('官网检索已完成；候选链接尚未获取正文', exact=True).wait_for(timeout=30000)
            ready(page)
            with Store(default_db()) as store:
                existing = {r[0] for r in store.db.execute('SELECT url FROM documents')}
                candidates = [dict(r) for r in store.db.execute("SELECT * FROM candidates WHERE origin LIKE 'ctld search:%'")]
                selected = next(c for c in candidates if c['url'] not in existing)
            card = page.locator('article').filter(has=page.get_by_role('link', name=selected['title'], exact=True))
            assert card.count() == 1
            card.get_by_role('button', name='获取公开正文', exact=True).click()
            page.get_by_text('正文获取已返回，请查看该链接的状态', exact=True).wait_for(timeout=30000)
            ready(page)
            assert '正文已入库' in card.inner_text()
            with Store(default_db()) as store:
                row = dict(store.db.execute('SELECT * FROM candidates WHERE url=?', (selected['url'],)).fetchone())
                assert row['status'] == 'fetched' and row['document_id']
                assert store.db.execute('SELECT body FROM documents WHERE id=?', (row['document_id'],)).fetchone()[0]
            page.screenshot(path=str(ROOT / 'output/playwright/phase2-discovery.png'), full_page=True)
            OUT.joinpath('ui-discovery-result.json').write_text(json.dumps({'search_candidates': len(candidates), 'chosen_candidate': row, 'body_in_database': True}, ensure_ascii=False, indent=2))
        OUT.joinpath(f'ui-{args.stage}-after.txt').write_text(page.locator('main.campus-radar').aria_snapshot())
        browser.close()
    print(json.dumps({'stage': args.stage, 'status': 'passed'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
