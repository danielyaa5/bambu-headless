// SPDX-License-Identifier: AGPL-3.0-only
// In-process ABI fixture. No networking, account data or real printer commands.
#include "vendor/bambu_networking.hpp"

static BBL::OnMessageFn message;
static BBL::OnLocalConnectedFn connected;
static std::string scenario;

static void status(const std::string &state, const std::string &job="fixture.gcode", int error=0, int bed_target=-1) {
    std::string extra=bed_target>=0?(",\"bed_temper\":25,\"bed_target_temper\":"+std::to_string(bed_target)):"";
    message("TEST_PRINTER", "{\"print\":{\"command\":\"push_status\",\"gcode_state\":\""+state+
            "\",\"subtask_name\":\""+job+"\",\"print_error\":"+std::to_string(error)+extra+"}}");
}

extern "C" {
std::string bambu_network_get_version() { return "02.08.02.60"; }
void *bambu_network_create_agent(std::string value) { scenario=value; return &scenario; }
int bambu_network_set_config_dir(void *, std::string) { return 0; }
int bambu_network_set_cert_file(void *, std::string, std::string) { return 0; }
int bambu_network_set_country_code(void *, std::string) { return 0; }
int bambu_network_set_on_local_connect_fn(void *, BBL::OnLocalConnectedFn fn) { connected=fn; return 0; }
int bambu_network_set_on_printer_connected_fn(void *, BBL::OnPrinterConnectedFn) { return 0; }
int bambu_network_set_on_local_message_fn(void *, BBL::OnMessageFn fn) { message=fn; return 0; }
int bambu_network_set_on_message_fn(void *, BBL::OnMessageFn) { return 0; }
int bambu_network_start(void *) { return 0; }
int bambu_network_connect_printer(void *, std::string, std::string, std::string, std::string, bool) {
    connected(0,"",""); return 0;
}
int bambu_network_disconnect_printer(void *) { return 0; }
int bambu_network_update_cert(void *) { return 0; }
void bambu_network_install_device_cert(void *, std::string, bool) {
    if(scenario=="changed-job")status("RUNNING","replacement.gcode");
    if(scenario=="changed-state")status("FINISH");
    message("TEST_PRINTER","device_cert_installed");
}
int bambu_network_send_message_to_printer(void *, std::string, std::string payload, int, int) {
    if(payload.find("pushall")!=std::string::npos) {
        if(scenario=="start"||scenario=="bed_temp")status("FINISH","old.gcode");
        else if(scenario=="resume")status("PAUSE");
        else if(scenario=="resume-error")status("PAUSE","fixture.gcode",123);
        else status("RUNNING");
        return 0;
    }
    if(payload.find("gcode_line")!=std::string::npos) {
        if(scenario=="rejected") {
            message("TEST_PRINTER","{\"print\":{\"command\":\"gcode_line\",\"sequence_id\":\"42\",\"result\":\"FAIL\",\"err_code\":123}}");
            return 0;
        }
        message("TEST_PRINTER","{\"print\":{\"command\":\"gcode_line\",\"sequence_id\":\"42\",\"result\":\"SUCCESS\",\"err_code\":0}}");
        status("FINISH","old.gcode",0,55);
        return 0;
    }
    std::string command=payload.find("project_file")!=std::string::npos?"project_file":
        payload.find("resume")!=std::string::npos?"resume":
        payload.find("pause")!=std::string::npos?"pause":"stop";
    if(scenario=="rejected") {
        message("TEST_PRINTER","{\"print\":{\"command\":\""+command+"\",\"sequence_id\":\"42\",\"result\":\"FAIL\",\"err_code\":123}}");
        return 0;
    }
    message("TEST_PRINTER","{\"print\":{\"command\":\""+command+"\",\"sequence_id\":\"42\",\"result\":\"SUCCESS\",\"err_code\":0}}");
    status(command=="pause"?"PAUSE":command=="resume"?"RUNNING":command=="project_file"?"PREPARE":"FAILED");
    return 0;
}
}
