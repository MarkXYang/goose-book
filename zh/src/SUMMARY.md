# 目录

[前言](00_preface.md)

# 第一部分：基础篇 —— 无堆内存思维

- [第 1 章：为什么选择 Goose？](part1_foundations/ch01_why_goose.md)
- [第 2 章：熟悉的语法，克制的设计](part1_foundations/ch02_syntax_and_control_flow.md)
- [第 3 章：值的栖身之所：大小类别与数据栈](part1_foundations/ch03_where_values_live.md)
- [第 4 章：数组家族与字符串](part1_foundations/ch04_arrays_and_strings.md)

# 第二部分：引用、类型与内存安全

- [第 5 章：切片、引用与可选引用](part2_memory_and_types/ch05_slices_refs_optionals.md)
- [第 6 章：无标注的生命周期](part2_memory_and_types/ch06_lifetimes_without_annotations.md)
- [第 7 章：两种大小的枚举与标签分发](part2_memory_and_types/ch07_enums_in_two_sizes.md)
- [第 8 章：结构体、根结构体与非嵌套生命周期](part2_memory_and_types/ch08_structs_roots_and_pools.md)

# 第三部分：抽象、控制流与数据结构

- [第 9 章：函数、泛型与编译期展开](part3_abstraction_and_data/ch09_functions_generics_and_metaprogramming.md)
- [第 10 章：错误处理与跨帧返回](part3_abstraction_and_data/ch10_errors_and_long_distance_returns.md)
- [第 11 章：数据栈模型下的递归](part3_abstraction_and_data/ch11_recursion_under_the_stack_model.md)
- [第 12 章：相对引用与无指针数据结构](part3_abstraction_and_data/ch12_relative_refs_and_data_structures.md)
- [第 13 章：零拷贝持久化与受验证序列化](part3_abstraction_and_data/ch13_verified_serialization.md)

# 第四部分：并发、互操作与生态

- [第 14 章：无共享并发](part4_systems_and_ecosystem/ch14_share_nothing_concurrency.md)
- [第 15 章：与 C 语言的双向互操作](part4_systems_and_ecosystem/ch15_c_interoperability.md)
- [第 16 章：程序结构、包管理与工具链](part4_systems_and_ecosystem/ch16_programs_packages_and_tooling.md)
- [第 17 章：标准库巡礼](part4_systems_and_ecosystem/ch17_standard_library_tour.md)

# 第五部分：性能工程

- [第 18 章：索引类型、越界检查消除与 `checkfree`](part5_performance/ch18_index_types_and_checkfree.md)
- [第 19 章：编写高性能 Goose 代码](part5_performance/ch19_writing_fast_goose.md)

# 第六部分：综合实战与编译器内幕

- [第 20 章：实战 1 —— 紧凑型 JSON 解析器与二进制缓存](part6_projects_and_internals/ch20_project_json_parser.md)
- [第 21 章：实战 2 —— 多线程图像处理流水线](part6_projects_and_internals/ch21_project_parallel_pipeline.md)
- [第 22 章：实战 3 —— 构建实时交互式应用程序](part6_projects_and_internals/ch22_project_interactive_app.md)
- [第 23 章：深入底层 —— Goose 编译器工作原理](part6_projects_and_internals/ch23_inside_the_compiler.md)

# 附录

- [附录 A：快速参考卡片](appendices/app_a_quick_reference.md)
- [附录 B：写给 C++ 与 Rust 程序员的 Goose 指南](appendices/app_b_from_cpp_and_rust.md)
- [附录 C：编译器诊断信息实战手册](appendices/app_c_compiler_diagnostics.md)
- [附录 D：语言现状、已知限制与延伸阅读](appendices/app_d_limitations_and_roadmap.md)
