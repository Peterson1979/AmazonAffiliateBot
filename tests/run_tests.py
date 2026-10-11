"""
Stand-alone test runner for AmazonAffiliateBot (V2) using Python's built-in unittest module.
Requires no external dependencies.
"""

import unittest
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Import all test cases
from tests.test_prompts import TestPrompts
from tests.test_generator import TestGeneratorAndValidator
from tests.test_cache import TestProductCache
from tests.test_package import TestContentPackage
from tests.test_publisher import TestPublisher
from tests.test_universal_architecture import TestUniversalGroundingArchitecture
from tests.test_publishing_queue import TestPublishingQueue, TestQueuePublisher
from tests.test_discovery_and_replenishment import TestDiscoveryAndReplenishment
from tests.test_dashboard import TestDashboard
from tests.test_content_quality_improvement import TestContentQualityImprovement


def suite():
    test_suite = unittest.TestSuite()
    test_suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(TestPrompts))
    test_suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(TestGeneratorAndValidator))
    test_suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(TestProductCache))
    test_suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(TestContentPackage))
    test_suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(TestPublisher))
    test_suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(TestUniversalGroundingArchitecture))
    test_suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(TestPublishingQueue))
    test_suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(TestQueuePublisher))
    test_suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(TestDiscoveryAndReplenishment))
    test_suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(TestDashboard))
    test_suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(TestContentQualityImprovement))
    return test_suite



if __name__ == "__main__":
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite())
    sys.exit(0 if result.wasSuccessful() else 1)
