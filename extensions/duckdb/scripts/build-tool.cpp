// Metadata format adapted from duckdb-geosupport (MIT); see licenses/.
#include <fstream>
#include <iostream>
#include <iterator>
#include <stdexcept>
#include <string>

static std::string Read(const char *path) {
    std::ifstream input(path, std::ios::binary);
    if (!input) throw std::runtime_error(std::string("Cannot read ") + path);
    return {std::istreambuf_iterator<char>(input), {}};
}
static void Field(std::ostream &output, const std::string &value) {
    if (value.size() > 32) throw std::runtime_error("Metadata field too long");
    output << value << std::string(32 - value.size(), '\0');
}
int main(int argc, char **argv) {
    try {
        if (argc < 4) throw std::runtime_error("Usage: build-tool sql INPUT OUTPUT | metadata INPUT OUTPUT PLATFORM VERSION");
        const std::string command = argv[1], bytes = Read(argv[2]);
        std::ofstream output(argv[3], std::ios::binary);
        if (!output) throw std::runtime_error("Cannot create output");
        if (command == "sql" && argc == 4) {
            output << "#pragma once\ninline constexpr char WHEREABOUTS_SQL[] = {\n";
            for (unsigned char byte : bytes) output << unsigned(byte) << ',';
            output << "0};\n";
        } else if (command == "metadata" && argc == 6) {
            if (!*argv[4] || !*argv[5]) throw std::runtime_error("Empty platform or version");
            output << bytes;
            const unsigned char header[] = {0, 147, 4, 16};
            output.write(reinterpret_cast<const char *>(header), sizeof(header));
            output << "duckdb_signature";
            output.put(static_cast<char>(128)); output.put(4);
            const char *fields[] = {"", "", "", "C_STRUCT", argv[5], "v1.2.0", argv[4], "4"};
            for (const auto *field : fields) Field(output, field);
            output << std::string(256, '\0');
        } else throw std::runtime_error("Invalid arguments");
        output.close();
        if (!output) throw std::runtime_error("Cannot write output");
    } catch (const std::exception &error) {
        std::cerr << error.what() << '\n'; return 1;
    }
}
