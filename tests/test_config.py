import tempfile,unittest
from pathlib import Path
from cta_pipeline.config import load_dotenv

class DotenvTests(unittest.TestCase):
    def test_loads_common_syntax_without_overriding_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/".env"; path.write_text("# comment\nOPENAI_API_KEY='from file'\nexport OPENAI_MODEL=demo\nEXISTING=replaced\n",encoding="utf-8")
            environ={"EXISTING":"process"}; self.assertTrue(load_dotenv(path,environ))
        self.assertEqual(environ,{"OPENAI_API_KEY":"from file","OPENAI_MODEL":"demo","EXISTING":"process"})
    def test_missing_file_is_optional_and_invalid_lines_fail(self):
        self.assertFalse(load_dotenv("/definitely/missing/.env",{}))
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/".env"; path.write_text("not an assignment",encoding="utf-8")
            with self.assertRaises(ValueError): load_dotenv(path,{})
