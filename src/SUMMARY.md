# Summary

[Preface](00_preface.md)

# Part I: Foundations — Thinking Without a Heap

- [Why Goose?](part1_foundations/ch01_why_goose.md)
- [Familiar Syntax, Deliberate Choices](part1_foundations/ch02_syntax_and_control_flow.md)
- [Where Values Live: Size Classes and Data Stacks](part1_foundations/ch03_where_values_live.md)
- [The Array Family and Strings](part1_foundations/ch04_arrays_and_strings.md)

# Part II: References, Types, and Safe Memory

- [Slices, References, and Optionals](part2_memory_and_types/ch05_slices_refs_optionals.md)
- [Lifetimes Without Annotations](part2_memory_and_types/ch06_lifetimes_without_annotations.md)
- [Enums in Two Sizes and Tag Dispatch](part2_memory_and_types/ch07_enums_in_two_sizes.md)
- [Structs, Root Structs, and Non-Nested Lifetimes](part2_memory_and_types/ch08_structs_roots_and_pools.md)

# Part III: Abstraction, Control, and Data Structures

- [Functions, Generics, and Compile-Time Expansion](part3_abstraction_and_data/ch09_functions_generics_and_metaprogramming.md)
- [Error Handling and Long-Distance Returns](part3_abstraction_and_data/ch10_errors_and_long_distance_returns.md)
- [Recursion Under the Stack Model](part3_abstraction_and_data/ch11_recursion_under_the_stack_model.md)
- [Relative References and Pointer-Free Data Structures](part3_abstraction_and_data/ch12_relative_refs_and_data_structures.md)
- [Zero-Copy Persistence and Verified Serialization](part3_abstraction_and_data/ch13_verified_serialization.md)

# Part IV: Concurrency, Interop, and Ecosystem

- [Share-Nothing Concurrency](part4_systems_and_ecosystem/ch14_share_nothing_concurrency.md)
- [Interoperating with C](part4_systems_and_ecosystem/ch15_c_interoperability.md)
- [Program Structure, Packages, and Tooling](part4_systems_and_ecosystem/ch16_programs_packages_and_tooling.md)
- [Tour of the Standard Library](part4_systems_and_ecosystem/ch17_standard_library_tour.md)

# Part V: Performance Engineering

- [Index Types, Bounds-Check Elimination, and `checkfree`](part5_performance/ch18_index_types_and_checkfree.md)
- [Writing Fast Goose](part5_performance/ch19_writing_fast_goose.md)

# Part VI: Capstone Projects & Under the Hood

- [Capstone 1: A Compact JSON Parser and Binary Cache](part6_projects_and_internals/ch20_project_json_parser.md)
- [Capstone 2: A Multi-Threaded Image Processing Pipeline](part6_projects_and_internals/ch21_project_parallel_pipeline.md)
- [Capstone 3: Building a Real-Time Interactive Application](part6_projects_and_internals/ch22_project_interactive_app.md)
- [Under the Hood: How the Goose Compiler Works](part6_projects_and_internals/ch23_inside_the_compiler.md)

# Appendices

- [Appendix A: Quick Reference](appendices/app_a_quick_reference.md)
- [Appendix B: Goose for C++ and Rust Programmers](appendices/app_b_from_cpp_and_rust.md)
- [Appendix C: Field Guide to Compiler Diagnostics](appendices/app_c_compiler_diagnostics.md)
- [Appendix D: Language Status, Limitations, and Further Reading](appendices/app_d_limitations_and_roadmap.md)
