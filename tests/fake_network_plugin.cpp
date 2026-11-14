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
int bambu_network_init_log(void *) { return 0; }
int bambu_network_set_config_dir(void *, std::string) { return 0; }
int bambu_network_set_cert_file(void *, std::string, std::string) { return 0; }
int bambu_network_set_country_code(void *, std::string) { return 0; }
int bambu_network_set_get_country_code_fn(void *, BBL::GetCountryCodeFn) { return 0; }
int bambu_network_set_extra_http_header(void *, std::map<std::string, std::string>) { return 0; }
int bambu_network_set_on_local_connect_fn(void *, BBL::OnLocalConnectedFn fn) { connected=fn; return 0; }
int bambu_network_set_on_printer_connected_fn(void *, BBL::OnPrinterConnectedFn) { return 0; }
int bambu_network_set_on_local_message_fn(void *, BBL::OnMessageFn fn) { message=fn; return 0; }
int bambu_network_set_on_message_fn(void *, BBL::OnMessageFn) { return 0; }
int bambu_network_start(void *) { return 0; }
int bambu_network_bind_detect(void *, std::string, std::string, BBL::detectResult &result) {
    result.dev_id=scenario=="wrong-serial"?"OTHER_PRINTER":"TEST_PRINTER";
    result.model_id=(scenario=="p1s"||scenario=="status-vt-tray")?"C12":"O1D";
    return scenario=="detection-failed"?-1:0;
}
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
        if(scenario=="status-details") {
            message("TEST_PRINTER", R"({"print":{
                "command":"push_status","gcode_state":"RUNNING","subtask_name":"fixture.gcode",
                "print_error":0,"bed_temper":60.5,"nozzle_temper":215.25,
                "ams":{"ams":[{"id":"0","tray":[{"id":"1","tray_type":"PLA",
                    "tray_color":"FFFFFFFF","tray_info_idx":"GFA00","private_field":"omit"}]}]},
                "device":{"nozzle":{"info":[{"id":0,"diameter":0.4,"type":"hardened_steel"}]},
                          "plate":{"base":4,"cali2d_id":"","cur_id":"P0101","mat":1,"tar_id":"","private_field":"omit"}},
                "xcam":{"buildplate_marker_detector":false,"private_field":"omit"},
                "private_field":"omit"
            }})");
            return 0;
        }
        if(scenario=="status-vir-slot") {
            message("TEST_PRINTER", R"({"print":{
                "command":"push_status","gcode_state":"RUNNING","subtask_name":"fixture.gcode",
                "print_error":0,"bed_temper":60.5,"nozzle_temper":215.25,
                "vir_slot":[{"id":"254","tray_type":"PETG","tray_color":"FFFFFFFF","tray_info_idx":"GFG99"},
                            {"id":"255","tray_type":"TPU","tray_color":"F98C36FF","tray_info_idx":"GFU99"}],
                "device":{"nozzle":{"info":[{"id":0,"diameter":0.4,"type":"hardened_steel"},{"id":1,"diameter":0.4,"type":"hardened_steel"}]}}
            }})");
            return 0;
        }
        if(scenario=="status-vt-tray") {
            message("TEST_PRINTER", R"({"print":{
                "command":"push_status","gcode_state":"RUNNING","subtask_name":"fixture.gcode",
                "print_error":0,"bed_temper":60.5,"nozzle_temper":215.25,
                "vt_tray":{"id":"254","tray_type":"PETG","tray_color":"FFFFFFFF","tray_info_idx":"GFG99"},
                "nozzle_diameter":"0.4"
            }})");
            return 0;
        }
        if(scenario=="start"||scenario=="start-record"||scenario=="no-ams-record"||scenario=="bed_temp")status("FINISH","old.gcode");
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
int bambu_network_get_user_tasks(void *, BBL::TaskQueryParams q, std::string *body) {
    if(scenario=="history-fail")return -1;
    *body="{\"total\":1,\"hits\":[{\"id\":9001,\"title\":\"fixture.gcode\",\"status\":2,\"startTime\":\"2026-11-07T00:00:00Z\",\"endTime\":\"2026-11-07T00:10:00Z\",\"weight\":5.5,\"length\":180,\"costTime\":600,\"mode\":\"lan_file\",\"deviceModel\":\"H2D\",\"deviceName\":\"3DP\",\"plateIndex\":1,\"bedType\":\"textured_plate\",\"failedType\":0,\"secretToken\":\"DO_NOT_LEAK\",\"amsDetailMapping\":[{\"ams\":5,\"filamentId\":\"GFB99\",\"filamentType\":\"ABS\",\"nozzleId\":1,\"sourceColor\":\"F98C36FF\",\"targetColor\":\"F98C36FF\",\"weight\":5.5,\"extraSecret\":\"NO\"}]}]}";
    return q.limit>0?0:-1;
}
int bambu_network_start_local_print_with_record(void *, BBL::PrintParams params, BBL::OnUpdateStatusFn update_fn, BBL::WasCancelledFn cancel_fn, BBL::OnWaitFn wait_fn) {
    if(params.project_name.empty()||params.task_name.empty()||params.config_filename.empty())return -2010;
    if(scenario=="no-ams-record" && params.task_use_ams)return -999;
    if(update_fn)update_fn(BBL::PrintingStageCreate,0,"");
    if(cancel_fn&&cancel_fn())return -18;
    if(update_fn)update_fn(BBL::PrintingStageRecord,0,"");
    status("PREPARE",params.project_name);
    if(wait_fn&&!wait_fn(BBL::PrintingStageWaitPrinter,"{\"job_id\":777001,\"wait_time\":30}"))
        return BAMBU_NETWORK_ERR_PRINT_SP_WAIT_PRINTER_FAILED;
    return 0;
}
}
