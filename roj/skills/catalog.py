"""Known skills and their workflows."""

SKILLS: dict[str, dict[str, str]] = {
    "news_digest": {
        "bootstrap": "roj.skills.news_digest.workflows:BOOTSTRAP",
        "run": "roj.skills.news_digest.workflows:RUN",
        "repair": "roj.skills.news_digest.workflows:REPAIR",
        "description": "Morning brief: overnight world events and their economic impact",
    },
    "lab_study": {
        "run": "roj.lab.study:STUDY",
        "description": "Lab team: measure models and prompt knobs, propose a Playbook (owner approves)",
    },
    "selfdev": {
        "run": "roj.lab.selfdev:SELFDEV",
        "description": "roj improves its own repository: one gated pull request per run",
    },
}


def ref(skill: str, kind: str) -> str:
    try:
        return SKILLS[skill][kind]
    except KeyError as e:
        raise KeyError(f"unknown skill or workflow {skill}.{kind}; known: {', '.join(SKILLS)}") from e
