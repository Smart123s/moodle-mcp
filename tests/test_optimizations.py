from datetime import datetime, timezone

from moodle_mcp import api


def test_get_active_courses(monkeypatch):
    now = int(datetime.now(timezone.utc).timestamp())
    mock_courses = [
        {"id": 1, "fullname": "Active Course 1", "shortname": "AC1", "startdate": now - 1000, "enddate": 0, "progress": 50},
        {"id": 2, "fullname": "Active Course 2", "shortname": "AC2", "startdate": now - 2000, "enddate": now + 50000, "progress": 20},
        {"id": 3, "fullname": "Old Course", "shortname": "OLD", "startdate": now - 600 * 86400, "enddate": now - 200 * 86400, "progress": 100},
    ]
    monkeypatch.setattr(api, "get_my_courses", lambda: mock_courses)

    active = api.get_active_courses()
    assert len(active) == 2
    active_ids = [c["id"] for c in active]
    assert 1 in active_ids
    assert 2 in active_ids
    assert 3 not in active_ids


def test_search_course_materials_filters_by_course_name(monkeypatch):
    mock_courses = [
        {"id": 101, "fullname": "Kvantitatív módszerek", "shortname": "KVANT", "startdate": 1000, "enddate": 0},
        {"id": 102, "fullname": "Algoritmusok", "shortname": "ALGO", "startdate": 1000, "enddate": 0},
        {"id": 103, "fullname": "Adatbázisok", "shortname": "DB", "startdate": 1000, "enddate": 0},
    ]
    monkeypatch.setattr(api, "get_my_courses", lambda: mock_courses)

    content_calls = []

    def mock_get_content(cid):
        content_calls.append(cid)
        return [
            {
                "id": 1,
                "name": "General",
                "summary": "Course intro",
                "modules": [{"id": 1, "name": "Syllabus", "modname": "resource", "url": "http://x"}],
            }
        ]

    monkeypatch.setattr(api, "get_course_content", mock_get_content)

    results = api.search_course_materials("kvantitatív módszerek")
    # Only course 101 should be queried because it matched the name directly!
    assert content_calls == [101]
    assert len(results) >= 1


def test_get_upcoming_deadlines_caps_status_checks(monkeypatch):
    now = int(datetime.now(timezone.utc).timestamp())
    # Create 10 upcoming assignments
    mock_assignments = [
        {
            "id": i,
            "name": f"Assignment {i}",
            "coursename": "Test Course",
            "courseid": 1,
            "duedate": now + 1000 * i,
        }
        for i in range(1, 11)
    ]
    monkeypatch.setattr(api, "get_assignments", lambda: mock_assignments)

    status_calls = []

    def mock_get_status(assignid, **kwargs):
        status_calls.append(assignid)
        return {
            "assignment_id": assignid,
            "status": "not_submitted",
            "submitted": False,
            "gradingstatus": "notgraded",
            "graded": False,
            "canedit": True,
            "cansubmit": True,
            "submission_status": None,
            "time_remaining": 1000,
            "duedate": now + 1000,
            "cutoffdate": 0,
            "extensionduedate": 0,
            "feedback": None,
            "grade": None,
        }

    monkeypatch.setattr(api, "get_assignment_status", mock_get_status)

    deadlines = api.get_upcoming_deadlines()
    assert len(deadlines) == 10
    # Status should be fetched for at most 5 items
    assert len(status_calls) <= 5


def test_get_course_progress_all_courses_makes_zero_extra_calls(monkeypatch):
    mock_courses = [
        {"id": 1, "fullname": "Course 1", "shortname": "C1", "progress": 85},
        {"id": 2, "fullname": "Course 2", "shortname": "C2", "progress": 10},
    ]
    monkeypatch.setattr(api, "get_my_courses", lambda: mock_courses)

    api_called = []

    def mock_api_call(*args, **kwargs):
        api_called.append(args)
        return {}

    monkeypatch.setattr(api, "get_moodle_api_data", mock_api_call)

    progress = api.get_course_progress(None)
    assert len(progress) == 2
    assert progress[0]["progress_percentage"] == 85
    assert progress[1]["progress_percentage"] == 10
    # No Moodle API calls should have been made
    assert len(api_called) == 0


def test_get_recent_activity_caps_courses(monkeypatch):
    now = int(datetime.now(timezone.utc).timestamp())
    mock_courses = [
        {"id": i, "fullname": f"Course {i}", "shortname": f"C{i}", "startdate": now - 100 * i, "enddate": 0}
        for i in range(1, 20)
    ]
    monkeypatch.setattr(api, "get_active_courses", lambda: mock_courses)

    updates_calls = []

    def mock_api_call(fn, params=None):
        if fn == api.APIFunction.core_course_get_updates_since:
            updates_calls.append(params.get("courseid"))
            return {"instances": []}
        return {}

    monkeypatch.setattr(api, "get_moodle_api_data", mock_api_call)
    monkeypatch.setattr(api, "to_json_file", lambda *a, **kw: None)
    monkeypatch.setattr(api, "_get_course_grades_overview", list)

    api.get_recent_activity()
    assert len(updates_calls) <= 5


def test_get_course_announcements_caps_forums(monkeypatch):
    mock_courses = [
        {"id": 1, "fullname": "Course 1", "shortname": "C1", "startdate": 100, "enddate": 0},
    ]
    monkeypatch.setattr(api, "get_active_courses", lambda: mock_courses)

    # 10 news forums
    forums = [{"id": i, "course": 1, "type": "news"} for i in range(1, 11)]

    discussion_calls = []

    def mock_api_call(fn, params=None):
        if fn == api.APIFunction.mod_forum_get_forums_by_courses:
            return {"forums": forums}
        if fn == api.APIFunction.mod_forum_get_forum_discussions:
            discussion_calls.append(params.get("forumid"))
            return {"discussions": []}
        return {}

    monkeypatch.setattr(api, "get_moodle_api_data", mock_api_call)
    monkeypatch.setattr(api, "to_json_file", lambda *a, **kw: None)

    api.get_course_announcements()
    assert len(discussion_calls) <= 5


def test_ask_moodle_hungarian_query(monkeypatch):
    now = int(datetime.now(timezone.utc).timestamp())
    mock_deadlines = [
        {
            "assignment_id": 10,
            "assignment_name": "Szorgalmi feladat",
            "course_name": "Kvantitatív módszerek",
            "duedate": now + 86400,
            "duedate_formatted": "2026-11-25T12:00:00+00:00",
            "submitted": False,
            "submission_status": None,
        },
        {
            "assignment_id": 20,
            "assignment_name": "Másik feladat",
            "course_name": "Másik tárgy",
            "duedate": now + 172800,
            "duedate_formatted": "2026-11-26T12:00:00+00:00",
            "submitted": False,
            "submission_status": None,
        },
    ]
    monkeypatch.setattr(api, "get_upcoming_deadlines", lambda: mock_deadlines)
    monkeypatch.setattr(api, "get_assignments", list)
    monkeypatch.setattr(api, "get_actionable_tasks", list)

    res = api.ask_moodle("mikorra kell a következő kvantitatív módszerek beadandó?")
    assert "upcoming_deadlines" in res["data_sources"]
    assert len(res["relevant_items"]) >= 1
    assert res["relevant_items"][0]["data"]["course_name"] == "Kvantitatív módszerek"
    assert "Szorgalmi feladat" in res["answer"]


def test_execute_parallel_preserves_oauth_context(monkeypatch):
    from mcp.server.auth.middleware.auth_context import AccessToken, AuthenticatedUser, auth_context_var
    from moodle_mcp import moodle

    # Clear environment & config
    monkeypatch.delenv("MOODLE_URL", raising=False)
    monkeypatch.delenv("MOODLE_TOKEN", raising=False)
    monkeypatch.setattr(moodle, "MOODLE_URL", None)
    monkeypatch.setattr(moodle, "MOODLE_TOKEN", None)
    monkeypatch.setattr(moodle, "get_moodle_url", lambda: None)
    monkeypatch.setattr(moodle, "get_moodle_token", lambda: None)

    # Set up mock OAuth provider
    class MockProvider:
        def get_moodle_url_for_access_token(self, token):
            return "https://moodle.test.edu/webservice/rest/server.php" if token == "valid-token" else None

        def get_moodle_token_for_access_token(self, token):
            return "test-wstoken" if token == "valid-token" else None

    mock_prov = MockProvider()
    monkeypatch.setattr("moodle_mcp.auth.get_active_provider", lambda: mock_prov)

    # Simulate request thread with active access token
    test_token = AccessToken(token="valid-token", client_id="client-1", scopes=["mcp"], expires_at=9999999999)
    auth_context_var.set(AuthenticatedUser(test_token))

    def worker_check(item):
        return (
            moodle.get_active_moodle_url(),
            moodle.get_active_moodle_token(),
        )

    # Execute in parallel worker threads
    results = api.execute_parallel(worker_check, [1, 2, 3])
    assert len(results) == 3
    for url, token in results:
        assert url == "https://moodle.test.edu/webservice/rest/server.php"
        assert token == "test-wstoken"


def test_get_my_courses_bilingual_alias(monkeypatch):
    now = int(datetime.now(timezone.utc).timestamp())
    mock_courses_raw = [
        {"id": 37362, "fullname": "Advanced Operations Research [I] (VEMIMAM176H/2026/27/1)", "shortname": "AOR", "startdate": now, "enddate": 0, "category": 1, "progress": 0, "format": "topics"},
        {"id": 30622, "fullname": "Operációkutatás (VEMIMAB244FO/2025/26/2)", "shortname": "OPKUT", "startdate": now - 300 * 86400, "enddate": 0, "category": 1, "progress": 0, "format": "topics"},
        {"id": 7542, "fullname": "Kvantitatív módszerek (magyar) (VEGTVEM144K/2026/27/1)", "shortname": "KVANT", "startdate": now, "enddate": 0, "category": 1, "progress": 0, "format": "topics"},
        {"id": 100, "fullname": "Adatbázisok", "shortname": "DB", "startdate": now, "enddate": 0, "category": 1, "progress": 0, "format": "topics"},
    ]
    monkeypatch.setattr(api, "get_moodle_api_data", lambda *a, **kw: mock_courses_raw)
    monkeypatch.setattr(api, "_get_user_id", lambda: 123)

    # Query in Hungarian "operációkutatás" should find both English & Hungarian courses, with current semester first
    matched = api.get_my_courses(query="operációkutatás")
    assert len(matched) == 2
    assert matched[0]["id"] == 37362
    assert matched[1]["id"] == 30622

    # Query "kvantitatív"
    matched_kvant = api.get_my_courses(query="kvantitatív")
    assert len(matched_kvant) == 1
    assert matched_kvant[0]["id"] == 7542


def test_get_assignments_chronological_sort(monkeypatch):
    now = int(datetime.now(timezone.utc).timestamp())
    mock_raw = {
        "courses": [
            {
                "id": 7542,
                "fullname": "Kvantitatív módszerek",
                "assignments": [
                    {"id": 3584, "name": "ARIMA modell", "duedate": now + 200000, "cutoffdate": 0},
                    {"id": 3572, "name": "K-közép klaszterezés", "duedate": now + 50000, "cutoffdate": 0},
                    {"id": 3575, "name": "Jövedelem vizsgálat", "duedate": now + 60000, "cutoffdate": 0},
                    {"id": 1001, "name": "Régi feladat", "duedate": now - 50000, "cutoffdate": 0},
                ],
            }
        ]
    }
    monkeypatch.setattr(api, "get_moodle_api_data", lambda *a, **kw: mock_raw)
    monkeypatch.setattr(api, "_ASSIGNMENTS_CACHE", None)

    assignments = api.get_assignments(courseids=[7542])
    # The nearest upcoming deadline must be first (index 0)
    assert assignments[0]["id"] == 3572
    assert assignments[1]["id"] == 3575
    assert assignments[2]["id"] == 3584
    # Past assignment at the end
    assert assignments[3]["id"] == 1001


def test_get_upcoming_deadlines_course_filter(monkeypatch):
    now = int(datetime.now(timezone.utc).timestamp())
    mock_assignments = [
        {"id": 1, "name": "Task 1", "coursename": "Kvantitatív módszerek (magyar) (VEGTVEM144K/2026/27/1)", "courseid": 7542, "duedate": now + 1000},
        {"id": 2, "name": "Task 2", "coursename": "Kvantitatív módszerek (magyar) (VEGTVEM144K/2026/27/1)", "courseid": 7542, "duedate": now + 2000},
        {"id": 3, "name": "Task 3", "coursename": "Más kurzus", "courseid": 9999, "duedate": now + 500},
    ]
    monkeypatch.setattr(api, "get_assignments", lambda **kw: mock_assignments)
    monkeypatch.setattr(api, "get_upcoming_events", lambda: [])
    monkeypatch.setattr(api, "get_assignment_status", lambda aid, **kw: {"submitted": False, "submission_status": "new"})

    # When filtering by "kvantitatív", Task 3 must be excluded even though its duedate was earlier
    deadlines = api.get_upcoming_deadlines(course="kvantitatív")
    assert len(deadlines) == 2
    assert deadlines[0]["assignment_id"] == 1
    assert deadlines[1]["assignment_id"] == 2


def test_get_course_content_extracts_description_and_summary(monkeypatch):
    mock_raw = [
        {
            "id": 10,
            "name": "General",
            "section": 0,
            "visible": 1,
            "uservisible": True,
            "summary": "<p>Course introductory overview.</p>",
            "modules": [
                {
                    "id": 621041,
                    "name": "General Infos",
                    "modname": "label",
                    "url": None,
                    "description": "<p dir=\"ltr\">On the 13th week of the semester, an in-class assessment is written, separately from the two parts. Each part contributes 50% to the final grade.</p>",
                    "contents": None,
                }
            ],
        }
    ]
    monkeypatch.setattr(api, "get_moodle_api_data", lambda *a, **kw: mock_raw)

    sections = api.get_course_content(37362)
    assert len(sections) == 1
    assert sections[0]["summary"] == "Course introductory overview."
    mod = sections[0]["modules"][0]
    assert mod["name"] == "General Infos"
    assert mod["modname"] == "label"
    assert "13th week of the semester" in mod["description"]
    assert "in-class assessment" in mod["description"]
    assert "<p" not in mod["description"]


def test_search_course_materials_matches_label_description(monkeypatch):
    mock_courses = [
        {"id": 37362, "fullname": "Advanced Operations Research [I] (VEMIMAM176H/2026/27/1)", "shortname": "AOR", "startdate": 1000, "enddate": 0},
    ]
    monkeypatch.setattr(api, "get_my_courses", lambda *a, **kw: mock_courses)

    mock_content = [
        {
            "name": "General",
            "section": 0,
            "summary": None,
            "modules": [
                {
                    "id": 621041,
                    "name": "General Infos",
                    "modname": "label",
                    "url": None,
                    "description": "On the 13th week of the semester, an in-class assessment is written, separately from the two parts.",
                    "contents": None,
                }
            ],
        }
    ]
    monkeypatch.setattr(api, "get_course_content", lambda cid: mock_content)

    results = api.search_course_materials("13th week assessment")
    assert len(results) == 1
    assert results[0]["title"] == "General Infos"
    assert "13th week of the semester" in results[0]["content"]



