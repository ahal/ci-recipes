from argparse import Namespace
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
from typing import List

import requests
from adr.query import run_query
from adr.util.memoize import memoize, memoized_property

HGMO_JSON_URL = "https://hg.mozilla.org/integration/{branch}/rev/{rev}?style=json"


class Status(Enum):
    PASS = 0
    FAIL = 1
    INTERMITTENT = 2


@dataclass
class Task:
    """Contains information pertaining to a single task."""
    label: str
    duration: int
    result: str
    classification: str

    @property
    def failed(self):
        return self.result in ('busted', 'exception', 'testfailed')


@dataclass
class LabelSummary:
    """Summarizes the overall state of a task label (across retriggers)."""
    label: str
    tasks: List[Task]

    def __post_init__(self):
        assert all(t.label == self.label for t in self.tasks)

    @property
    def classifications(self):
        return set(t.classification for t in self.tasks if t.failed)

    @property
    def results(self):
        return set(t.result for t in self.tasks)

    @memoized_property
    def status(self):
        overall_status = None
        for task in self.tasks:
            if task.failed:
                status = Status.FAIL
            else:
                status = Status.PASS

            if overall_status is None:
                overall_status = status
            elif status != overall_status:
                overall_status = Status.INTERMITTENT

        return overall_status


class Push:

    def __init__(self, rev, branch='autoland'):
        """A representation of a single push.

        Args:
            rev (str): Revision of the top-most commit in the push.
            branch (str): Branch to look on (default: autoland).
        """
        self.rev = rev
        self.branch = branch

    @property
    def backedoutby(self):
        """The revision of the commit which backs out this one or None.

        Returns:
            str or None: The commit revision which backs this push out (or None).
        """
        return self._hgmo.get('backedoutby') or None

    @property
    def backedout(self):
        """Whether the push was backed out or not.

        Returns:
            bool: True if this push was backed out.
        """
        return bool(self.backedoutby)

    @property
    def pushid(self):
        """The push id.

        Returns:
            int: The push id.
        """
        return self._hgmo['pushid']

    @memoized_property
    def parent(self):
        """Returns the parent push of this push.

        Returns:
            Push: A `Push` instance representing the parent push.
        """
        other = self
        while True:
            for rev in other._hgmo['parents']:
                parent = Push(rev)
                if parent.pushid != self.pushid:
                    return parent
                other = parent

    @memoized_property
    def tasks(self):
        """All tasks that ran on the push, including retriggers and backfills.

        Returns:
            list: A list of `Task` objects.
        """
        args = Namespace(rev=self.rev)
        data = run_query('push_results', args)['data']

        tasks = []
        for kwargs in data:
            # Do a bit of data sanitization.
            if any(a not in kwargs for a in ('label', 'duration', 'result', 'classification')):
                continue

            if kwargs['duration'] <= 0:
                continue

            tasks.append(Task(**kwargs))

        return tasks

    @property
    def task_labels(self):
        """The set of task labels that ran on this push.

        Returns:
            set: A set of task labels (str).
        """
        return set([t.label for t in self.tasks])

    @memoized_property
    def target_task_labels(self):
        """The set of all task labels that could possibly run on this push.

        Returns:
            set: A set of task labels.
        """
        return set(self._get_decision_artifact('target-tasks.json'))

    @memoized_property
    def scheduled_task_labels(self):
        """The set of task labels that were originally scheduled to run on this push.

        This excludes retriggers and backfills.

        Returns:
            set: A set of task labels (str).
        """
        tasks = self._get_decision_artifact('task-graph.json').values()
        return set([t['label'] for t in tasks])

    @property
    def unscheduled_task_labels(self):
        """The set of task labels from tasks that were not originally scheduled on
        the push (i.e they were scheduled via backfill or Add New Jobs).

        Returns:
            set: A set of task labels (str).
        """
        return self.task_labels - self.scheduled_task_labels

    @memoized_property
    def label_summaries(self):
        """All label summaries combining retriggers.

        Returns:
            dict: A dictionary of the form {<label>: [<LabelSummary>]}."""
        labels = defaultdict(list)
        for task in self.tasks:
            labels[task.label].append(task)
        labels = {label: LabelSummary(label, tasks) for label, tasks in labels.items()}
        return labels

    @memoized_property
    def duration(self):
        """The total duration of all tasks that ran on the push.

        Returns:
            int: Runtime in hours.
        """
        return int(sum(t.duration for t in self.tasks) / 3600)

    @memoized_property
    def scheduled_duration(self):
        """The total runtime of tasks excluding retriggers and backfills.

        Returns:
            int: Runtime in hours.
        """
        seen = set()
        duration = 0
        for task in self.tasks:
            if task.label not in seen:
                seen.add(task.label)
                duration += task.duration
        return int(duration / 3600)

    @memoized_property
    def candidate_regressions(self):
        """The set of task labels that are regression candidates for this push.

        A candidate regression is any task label for which at least one
        associated task failed (therefore including intermittents), and which
        is either not classified or fixed by commit.

        Returns:
            set: Set of task labels (str).
        """
        failclass = ('not classified', 'fixed by commit')
        candidate_regressions = set()
        for label, summary in self.label_summaries.items():
            if summary.status == Status.PASS:
                continue

            if all(c not in failclass for c in summary.classifications):
                continue

            candidate_regressions.add(label)
        return candidate_regressions

    @memoized_property
    def regressions(self):
        """All regressions, both likely and definite.

        Each regression is associated with an integer, which is the number of
        parent pushes that didn't run the label. A count of 0 means the label
        passed on the previous push. A count of 3 means there were three pushes
        between this one and the last time the task passed (so any one of them
        could have caused it). A count of 99 means that the maximum number of
        parents were searched without finding the task and we gave up.

        Returns:
            dict: A dict of the form {<label>: <int>}.
        """
        # The maximum number of parents to search. If the task wasn't run
        # on any of them, we assume there was no prior regression.
        max_depth = 5
        regressions = {}

        for label in self.candidate_regressions:
            count = 0
            other = self.parent
            prior_regression = False

            while True:
                if label in other.task_labels:
                    if other.label_summaries[label].status != Status.PASS:
                        prior_regression = True
                    break

                other = other.parent
                count += 1

            if not prior_regression:
                regressions[label] = count

        return regressions

    @property
    def possible_regressions(self):
        """The set of all task labels that may have been regressed by this push.

        A possible regression is a candidate_regression that didn't run on one or
        more parent pushes.

        Returns:
            set: Set of task labels (str).
        """
        return set([label for label, count in self.regressions.items() if count > 0])

    @property
    def likely_regressions(self):
        """The set of all task labels that were likely regressed by this push.

        A likely regression is a candidate_regression that both ran and passed
        on the immediate parent push. It still isn't a sure thing as the task
        could be intermittent.

        Returns:
            set: Set of task labels (str).
        """
        return set([label for label, count in self.regressions.items() if count == 0])

    @memoized_property
    def _decision_artifact_urls(self):
        """All artifact urls from the Decision task of this push.

        Returns:
            list: A list of urls.
        """
        return run_query('decision_artifacts', Namespace(rev=self.rev))['data'][0]['artifacts']

    @memoize
    def _get_decision_artifact(self, name):
        """Get an artifact from Decision task of this push.

        Args:
            name (str): Name of the artifact fetch.

        Returns:
            dict: JSON representation of the artifact.
        """
        for url in self._decision_artifact_urls:
            if url.rsplit('/', 1)[1] == name:
                return requests.get(url).json()

    @memoized_property
    def _hgmo(self):
        """A JSON dict obtained from hg.mozilla.org.

        Returns:
            dict: Information regarding this push.
        """
        url = HGMO_JSON_URL.format(branch=self.branch, rev=self.rev)
        return requests.get(url).json()