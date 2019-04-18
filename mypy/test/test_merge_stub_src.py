"""Type checker test cases"""

import os
import re
import sys
from typing import Dict, List, Set, Tuple

from mypy import build
from mypy.build import Graph
from mypy.errors import CompileError
from mypy.modulefinder import BuildSource, SearchPaths, FindModuleCache
from mypy.test.config import test_temp_dir, test_data_prefix
from mypy.test.data import DataDrivenTestCase, DataSuite
from mypy.test.helpers import (
    assert_string_arrays_equal,
    normalize_error_messages,
    update_testcase_output,
    parse_options,
)

# List of files that contain test case descriptions.
typecheck_files = ["check-stub-src-merge.test"]


class TypeCheckSuite(DataSuite):
    files = typecheck_files

    def run_case(self, testcase: DataDrivenTestCase) -> None:
        self.run_case_once(testcase)

    def run_case_once(self, testcase: DataDrivenTestCase) -> None:
        original_program_text = "\n".join(testcase.input)
        options = parse_options(original_program_text, testcase, 0)

        if any(p.startswith(os.path.join("tmp", "out")) for p, _ in testcase.files):
            direct_asts = self.load_data(
                options, original_program_text, testcase, to_merge=False
            )
        else:
            direct_asts = None
        merged_asts = self.load_data(
            options, original_program_text, testcase, to_merge=True
        )
        if direct_asts is not None:
            assert merged_asts.keys() == direct_asts.keys()
            for key in merged_asts:
                merged = merged_asts[key]
                direct = direct_asts[key]
                assert merged == direct

    def load_data(self, options, original_program_text, testcase, to_merge=False):
        folder = "in" if to_merge else "out"
        module_data = self.parse_module(original_program_text, folder)
        options.merge_stub_into_src = to_merge
        options.use_builtins_fixtures = True
        options.show_traceback = True
        options.strict_optional = True
        sources = []
        iterator = iter(module_data)
        no_next = None, None, None
        module_name, program_path, program_text = next(iterator, no_next)
        while module_name:
            source = BuildSource(program_path, module_name, program_text)
            sources.append(source)
            module_name, program_path, program_text = next(iterator, no_next)
            if options.merge_stub_into_src is True:
                if source.path.endswith(".pyi") and source.module == module_name:
                    src = BuildSource(
                        program_path, module_name, program_text, merge_with=source
                    )
                    sources[-1] = src
                    module_name, program_path, program_text = next(iterator, no_next)
        plugin_dir = os.path.join(test_data_prefix, "plugins")
        sys.path.insert(0, plugin_dir)
        res = None
        try:
            res = build.build(
                sources=sources,
                options=options,
                alt_lib_path=os.path.join(test_temp_dir),
            )
            a = res.errors
        except CompileError as e:
            a = e.messages
        finally:
            assert sys.path[0] == plugin_dir
            del sys.path[0]
        if to_merge:
            if testcase.normalize_output:
                a = normalize_error_messages(a)
            msg = "Unexpected type checker output ({}, line {})"
            output = testcase.output
            if output != a and testcase.config.getoption("--update-data", False):
                update_testcase_output(testcase, a)
            assert_string_arrays_equal(
                output, a, msg.format(testcase.file, testcase.line)
            )
        if res:
            if options.cache_dir != os.devnull:
                self.verify_cache(module_data, res.errors, res.manager, res.graph)
        ast_mod_to_graph = {}
        for source in sources:
            full_ast_str = str(res.graph[source.module].tree)
            repr_path = source.path.replace(os.sep, "/")
            file_path_regex = re.compile(
                r"\s+{}\s+^".format(re.escape(repr_path)), re.MULTILINE
            )
            ast_with_no_file_path = file_path_regex.sub("\n", full_ast_str)
            line_nr = re.compile(r"(\w+):\d+")
            ast_str = line_nr.sub(r"\1", ast_with_no_file_path)
            ast_mod_to_graph[source.module] = ast_str
        return ast_mod_to_graph

    def verify_cache(
        self,
        module_data: List[Tuple[str, str, str]],
        a: List[str],
        manager: build.BuildManager,
        graph: Graph,
    ) -> None:
        # There should be valid cache metadata for each module except
        # for those that had an error in themselves or one of their
        # dependencies.
        error_paths = self.find_error_message_paths(a)
        busted_paths = {
            m.path for id, m in manager.modules.items() if graph[id].transitive_error
        }
        modules = self.find_module_files(manager)
        modules.update({module_name: path for module_name, path, text in module_data})
        missing_paths = self.find_missing_cache_files(modules, manager)
        # We would like to assert error_paths.issubset(busted_paths)
        # but this runs into trouble because while some 'notes' are
        # really errors that cause an error to be marked, many are
        # just notes attached to other errors.
        assert (
            error_paths or not busted_paths
        ), "Some modules reported error despite no errors"
        if not missing_paths == busted_paths:
            raise AssertionError(
                "cache data discrepancy %s != %s" % (missing_paths, busted_paths)
            )

    def find_error_message_paths(self, a: List[str]) -> Set[str]:
        hits = set()
        for line in a:
            m = re.match(r"([^\s:]+):(\d+:)?(\d+:)? (error|warning|note):", line)
            if m:
                p = m.group(1)
                hits.add(p)
        return hits

    def find_module_files(self, manager: build.BuildManager) -> Dict[str, str]:
        modules = {}
        for id, module in manager.modules.items():
            modules[id] = module.path
        return modules

    def find_missing_cache_files(
        self, modules: Dict[str, str], manager: build.BuildManager
    ) -> Set[str]:
        ignore_errors = True
        missing = {}
        for id, path in modules.items():
            meta = build.find_cache_meta(id, path, manager)
            if not build.validate_meta(meta, id, path, ignore_errors, manager):
                missing[id] = path
        return set(missing.values())

    def parse_module(self, program_text: str, folder) -> List[Tuple[str, str, str]]:
        """Return a list of tuples (module name, file name, program text). """
        m = re.search(
            r"# modules: ([a-zA-Z0-9_. ]+)$", program_text, flags=re.MULTILINE
        )
        if m:
            in_path = os.path.join(test_temp_dir, folder)
            # The test case wants to use a non-default main
            # module. Look up the module and give it as the thing to
            # analyze.
            module_names = m.group(1)
            out = []
            search_paths = SearchPaths((in_path,), (), (), ())
            cache = FindModuleCache(search_paths)
            for module_name in module_names.split(" "):
                path = cache.find_module(module_name)
                assert path is not None, "Can't find ad hoc case file {} in {}".format(
                    in_path, module_names
                )
                for p in path if isinstance(path, tuple) else (path,):
                    with open(p, encoding="utf8") as f:
                        program_text = f.read()
                    out.append((module_name, p, program_text))
            return out
        else:
            raise ValueError("no modules defined")
